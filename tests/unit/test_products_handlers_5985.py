"""Обработчики компонентов: по одному на каждый ``Component.type`` (#5985).

Проверяется не «команда вызвалась», а контракт ADR: идемпотентность (второй
раз ничего не делаем), сохранность чужих данных (settings.json, cordis.patch.yml)
и отказ до установки, если sha256 не сходится. Внешние команды — фейковый
runner: тест видит ровно тот argv, который ушёл бы в uv/claude/установщик.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from skillery_cli.products.download import ChecksumError
from skillery_cli.products.handlers import get_handler
from skillery_cli.products.handlers.base import (
    InstallContext,
)
from skillery_cli.products.paths import home_paths
from skillery_cli.products.plan import Artifact, Component
from skillery_cli.products.runner import CommandResult, FakeRunner
from skillery_cli.products.state import InstalledState


@pytest.fixture(autouse=True)
def _clean_product_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DSH_HOME", "DSH_PROFILE", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def ctx() -> InstallContext:
    return InstallContext(
        paths=home_paths(),
        runner=FakeRunner(),
        state=InstalledState.load(),
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _served_archive(tmp_path: Path, entries: dict[str, bytes]) -> tuple[str, str]:
    data = _zip_bytes(entries)
    path = tmp_path / "asset.zip"
    path.write_bytes(data)
    return path.as_uri(), hashlib.sha256(data).hexdigest()


def _artifact(
    kind: str,
    *,
    url: str = "",
    sha256: str = "",
    install: dict | None = None,
    skill_ref: str = "",
) -> Artifact:
    return Artifact(
        kind=kind,
        os="linux",
        arch="amd64",
        url=url,
        sha256=sha256,
        skill_ref=skill_ref,
        install=install or {},
    )


def _run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


# ── cli ──────────────────────────────────────────────────────────────────────

def test_cli_uv_tool_installs_package(ctx: InstallContext) -> None:
    ctx.runner = FakeRunner(
        results={
            ("uv", "tool", "list"): CommandResult(argv=("uv", "tool", "list"), returncode=0),
            ("uv", "tool", "install", "skillery-cli"): CommandResult(
                argv=("uv", "tool", "install", "skillery-cli"), returncode=0
            ),
        }
    )
    component = Component(
        key="skillery-cli",
        type="cli",
        artifact=_artifact("uv-tool", install={"package": "skillery-cli"}),
    )
    result = _run(get_handler("cli").install(component, ctx))

    assert result.status == "installed"
    assert ("uv", "tool", "install", "skillery-cli") in ctx.runner.calls


def test_cli_uv_tool_is_idempotent(ctx: InstallContext) -> None:
    ctx.runner = FakeRunner(
        results={
            ("uv", "tool", "list"): CommandResult(
                argv=("uv", "tool", "list"), returncode=0, stdout="skillery-cli v0.5.94\n"
            )
        }
    )
    component = Component(
        key="skillery-cli",
        type="cli",
        artifact=_artifact("uv-tool", install={"package": "skillery-cli"}),
    )
    result = _run(get_handler("cli").install(component, ctx))

    assert result.status == "already-installed"
    assert ctx.runner.calls == [("uv", "tool", "list")]


def test_cli_binary_installs_into_bin_dir(ctx: InstallContext, tmp_path: Path) -> None:
    payload = b"#!/bin/sh\necho hi\n"
    source = tmp_path / "mytool"
    source.write_bytes(payload)
    component = Component(
        key="mytool-cli",
        type="cli",
        artifact=_artifact(
            "binary",
            url=source.as_uri(),
            sha256=hashlib.sha256(payload).hexdigest(),
            install={"bin": "mytool"},
        ),
    )
    result = _run(get_handler("cli").install(component, ctx))

    target = ctx.paths.bin_dir / "mytool"
    assert result.status == "installed"
    assert target.read_bytes() == payload
    assert target.stat().st_mode & stat.S_IXUSR


def test_cli_binary_refuses_bad_sha_and_writes_nothing(
    ctx: InstallContext, tmp_path: Path
) -> None:
    source = tmp_path / "mytool"
    source.write_bytes(b"payload")
    component = Component(
        key="mytool-cli",
        type="cli",
        artifact=_artifact(
            "binary",
            url=source.as_uri(),
            sha256="0" * 64,
            install={"bin": "mytool"},
        ),
    )
    with pytest.raises(ChecksumError):
        _run(get_handler("cli").install(component, ctx))
    assert not (ctx.paths.bin_dir / "mytool").exists()


def test_cli_binary_from_archive_picks_bin_member(
    ctx: InstallContext, tmp_path: Path
) -> None:
    url, digest = _served_archive(
        tmp_path, {"pkg/bin/mytool": b"#!bin", "pkg/README": b"docs"}
    )
    component = Component(
        key="mytool-cli",
        type="cli",
        artifact=_artifact("binary", url=url, sha256=digest, install={"bin": "mytool"}),
    )
    result = _run(get_handler("cli").install(component, ctx))
    assert result.status == "installed"
    assert (ctx.paths.bin_dir / "mytool").read_bytes() == b"#!bin"


# ── mcp ──────────────────────────────────────────────────────────────────────

def test_mcp_stdio_registers_with_claude_and_env_names_only(ctx: InstallContext) -> None:
    ctx.runner = FakeRunner(
        results={
            ("claude", "mcp", "get", "docs-server"): CommandResult(
                argv=("claude", "mcp", "get", "docs-server"), returncode=1
            )
        }
    )
    component = Component(
        key="docs-server",
        type="mcp",
        artifact=_artifact(
            "mcp-spec",
            install={
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "docs-mcp"],
                "env_keys": ["API_KEY", "ORG_ID"],
            },
        ),
    )
    result = _run(get_handler("mcp").install(component, ctx))

    assert result.status == "installed"
    assert ctx.runner.calls[-1] == (
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "docs-server",
            "-e",
            "API_KEY=",
            "-e",
            "ORG_ID=",
            "--",
            "npx",
            "-y",
            "docs-mcp",
        )


def test_mcp_http_registers_url(ctx: InstallContext) -> None:
    ctx.runner = FakeRunner(
        results={
            ("claude", "mcp", "get", "sentry"): CommandResult(
                argv=("claude", "mcp", "get", "sentry"), returncode=1
            )
        }
    )
    component = Component(
        key="sentry",
        type="mcp",
        artifact=_artifact(
            "mcp-spec",
            install={"transport": "http", "url": "https://mcp.example/mcp"},
        ),
    )
    _run(get_handler("mcp").install(component, ctx))
    assert ctx.runner.calls[-1] == (
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "--transport",
        "http",
        "sentry",
        "https://mcp.example/mcp",
    )


def test_mcp_verify_uses_claude_get(ctx: InstallContext) -> None:
    ctx.runner = FakeRunner(
        results={
            ("claude", "mcp", "get", "sentry"): CommandResult(
                argv=("claude", "mcp", "get", "sentry"), returncode=0
            )
        }
    )
    component = Component(
        key="sentry",
        type="mcp",
        artifact=_artifact("mcp-spec", install={"transport": "http", "url": "https://x"}),
    )
    result = _run(get_handler("mcp").install(component, ctx))
    assert result.status == "already-installed"


# ── plugin-claude ────────────────────────────────────────────────────────────

def test_plugin_claude_preserves_settings(
    ctx: InstallContext, tmp_path: Path
) -> None:
    url, digest = _served_archive(tmp_path, {"limits-cache/plugin.json": b"{}"})
    ctx.paths.claude_dir.mkdir(parents=True, exist_ok=True)
    ctx.paths.claude_settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "env": {"OTHER": "1", "CLAUDE_CODE_PLUGIN_DIRS": "/foreign/dir"},
            }
        ),
        encoding="utf-8",
    )
    component = Component(
        key="limits-cache",
        type="plugin-claude",
        artifact=_artifact("archive", url=url, sha256=digest, install={"name": "limits-cache"}),
    )
    result = _run(get_handler("plugin-claude").install(component, ctx))

    target = ctx.paths.claude_plugins / "limits-cache"
    assert result.status == "installed"
    assert (target / "plugin.json").exists()
    settings = json.loads(ctx.paths.claude_settings.read_text(encoding="utf-8"))
    assert settings["model"] == "opus"
    assert settings["env"]["OTHER"] == "1"
    dirs = settings["env"]["CLAUDE_CODE_PLUGIN_DIRS"].split(os.pathsep)
    assert "/foreign/dir" in dirs
    assert str(ctx.paths.claude_plugins) in dirs


def test_plugin_claude_is_idempotent(ctx: InstallContext, tmp_path: Path) -> None:
    url, digest = _served_archive(tmp_path, {"limits-cache/plugin.json": b"{}"})
    component = Component(
        key="limits-cache",
        type="plugin-claude",
        artifact=_artifact("archive", url=url, sha256=digest),
    )
    handler = get_handler("plugin-claude")
    _run(handler.install(component, ctx))
    settings_before = ctx.paths.claude_settings.read_bytes()

    calls = {"n": 0}

    def _counting(url: str, dest: Path, **kwargs) -> Path:  # noqa: ANN001
        calls["n"] += 1
        return ctx.downloader(url, dest, **kwargs)

    ctx.downloader = _counting
    result = _run(handler.install(component, ctx))

    assert result.status == "already-installed"
    assert calls["n"] == 0
    assert ctx.paths.claude_settings.read_bytes() == settings_before


def test_plugin_claude_remove_drops_dir_and_setting(ctx: InstallContext, tmp_path: Path) -> None:
    url, digest = _served_archive(tmp_path, {"limits-cache/plugin.json": b"{}"})
    component = Component(
        key="limits-cache",
        type="plugin-claude",
        artifact=_artifact("archive", url=url, sha256=digest),
    )
    handler = get_handler("plugin-claude")
    _run(handler.install(component, ctx))
    result = _run(handler.remove(component, ctx))

    assert result.status == "removed"
    assert not (ctx.paths.claude_plugins / "limits-cache").exists()
    settings = json.loads(ctx.paths.claude_settings.read_text(encoding="utf-8"))
    assert str(ctx.paths.claude_plugins) not in settings["env"]["CLAUDE_CODE_PLUGIN_DIRS"]


# ── plugin-dsh ───────────────────────────────────────────────────────────────

PLUGIN_JSON = json.dumps(
    {
        "id": "dsh-demo",
        "name": "demo",
        "version": "1.0.0",
        "entry": "index.js",
        "insert": [
            {"id": "speech-to-text", "name": "@scope/speech", "config": {"language": "ru"}}
        ],
    }
).encode()


def _write_patch(ctx: InstallContext) -> None:
    ctx.paths.dsh_profile.mkdir(parents=True, exist_ok=True)
    ctx.paths.dsh_patch.write_text(
        "# чужая раскладка\n"
        "- insert:\n"
        "    - id: other\n"
        "      name: '@scope/other'\n"
        "\n"
        "# хвост\n",
        encoding="utf-8",
    )


def test_plugin_dsh_adds_rows_and_keeps_foreign(ctx: InstallContext, tmp_path: Path) -> None:
    url, digest = _served_archive(
        tmp_path, {"demo/plugin.json": PLUGIN_JSON, "demo/index.js": b"code"}
    )
    _write_patch(ctx)
    component = Component(
        key="demo",
        type="plugin-dsh",
        artifact=_artifact("archive", url=url, sha256=digest),
    )
    result = _run(get_handler("plugin-dsh").install(component, ctx))

    text = ctx.paths.dsh_patch.read_text(encoding="utf-8")
    assert result.status == "installed"
    assert (ctx.paths.dsh_plugins / "demo" / "index.js").exists()
    assert "id: dsh-demo" in text
    assert "id: speech-to-text" in text
    assert "id: other" in text
    assert "# чужая раскладка" in text


def test_plugin_dsh_is_idempotent_by_id(ctx: InstallContext, tmp_path: Path) -> None:
    url, digest = _served_archive(
        tmp_path, {"demo/plugin.json": PLUGIN_JSON, "demo/index.js": b"code"}
    )
    _write_patch(ctx)
    component = Component(
        key="demo",
        type="plugin-dsh",
        artifact=_artifact("archive", url=url, sha256=digest),
    )
    handler = get_handler("plugin-dsh")
    _run(handler.install(component, ctx))
    first = ctx.paths.dsh_patch.read_bytes()
    result = _run(handler.install(component, ctx))

    assert result.status == "already-installed"
    assert ctx.paths.dsh_patch.read_bytes() == first
    assert ctx.paths.dsh_patch.read_text(encoding="utf-8").count("id: dsh-demo") == 1


def test_plugin_dsh_quotes_yaml_reserved_scalars(
    ctx: InstallContext, tmp_path: Path
) -> None:
    """Имена пакетов начинаются с ``@`` — в YAML это резерв, значение в кавычках.

    Диктовка ставит именно такие строки (``@deepseek-ai/…``): без кавычек
    разбор раскладки падает, и плагин не поднимается.
    """
    plugin_json = json.dumps(
        {
            "id": "dsh-quoted",
            "entry": "index.js",
            "insert": [
                {
                    "id": "hooks-claude-code",
                    "name": "@deepseek-ai/dsh-hooks-claude-code",
                    "config": {"configPath": "${pluginDir}/hooks.json"},
                }
            ],
        }
    ).encode()
    url, digest = _served_archive(
        tmp_path, {"quoted/plugin.json": plugin_json, "quoted/index.js": b"code"}
    )
    _write_patch(ctx)
    component = Component(
        key="quoted",
        type="plugin-dsh",
        artifact=_artifact("archive", url=url, sha256=digest),
    )
    _run(get_handler("plugin-dsh").install(component, ctx))

    text = ctx.paths.dsh_patch.read_text(encoding="utf-8")
    assert 'name: "@deepseek-ai/dsh-hooks-claude-code"' in text
    assert "name: @deepseek-ai" not in text
    # config — узел записи: ``config:`` своей строкой, ключи на два пробела
    # глубже. Без узла Harness прочитает поля как чужие для строки раскладки.
    assert "      config:\n" in text
    assert '        configPath: "' in text


def test_plugin_dsh_remove_removes_its_rows_only(ctx: InstallContext, tmp_path: Path) -> None:
    url, digest = _served_archive(
        tmp_path, {"demo/plugin.json": PLUGIN_JSON, "demo/index.js": b"code"}
    )
    _write_patch(ctx)
    component = Component(
        key="demo",
        type="plugin-dsh",
        artifact=_artifact("archive", url=url, sha256=digest),
    )
    handler = get_handler("plugin-dsh")
    _run(handler.install(component, ctx))
    result = _run(handler.remove(component, ctx))

    text = ctx.paths.dsh_patch.read_text(encoding="utf-8")
    assert result.status == "removed"
    assert "id: dsh-demo" not in text
    assert "id: speech-to-text" not in text
    assert "id: other" in text
    assert not (ctx.paths.dsh_plugins / "demo").exists()


# ── service ─────────────────────────────────────────────────────────────────

class _WritingRunner(FakeRunner):
    """Установщик «тихо» создаёт файл detect — как настоящий Inno Setup."""

    def __init__(self, detect: Path) -> None:
        super().__init__()
        self._detect = detect
        self.executable: bool | None = None

    def run(self, argv, *, timeout=None):  # noqa: ANN001, ANN201
        # Права проверяем ДО «запуска»: на POSIX файл без бита выполнения
        # subprocess отверг бы ещё до установки.
        self.executable = bool(os.stat(argv[0]).st_mode & 0o111)
        self.installer = Path(argv[0])
        result = super().run(argv, timeout=timeout)
        self._detect.parent.mkdir(parents=True, exist_ok=True)
        self._detect.write_text("installed", encoding="utf-8")
        return result


def test_service_marks_downloaded_installer_executable(
    ctx: InstallContext, tmp_path: Path
) -> None:
    """Скачанный установщик приходит без бита выполнения — перед запуском он есть."""
    payload = b"#!/bin/sh\nexit 0\n"
    installer = tmp_path / "setup.sh"
    installer.write_bytes(payload)
    runner = _WritingRunner(Path.home() / "Dictation" / "dictation.exe")
    ctx.runner = runner
    component = Component(
        key="dictation-service",
        type="service",
        artifact=_artifact(
            "installer",
            url=installer.as_uri(),
            sha256=hashlib.sha256(payload).hexdigest(),
            install={"args": [], "detect": {"file": "$HOME/Dictation/dictation.exe"}},
        ),
    )
    _run(get_handler("service").install(component, ctx))

    if os.name != "nt":
        assert runner.executable is True
    # Не системный /tmp (часто noexec), а кэш Skillery.
    assert ctx.paths.downloads_dir in runner.installer.parents


def test_service_runs_installer_with_args_and_checks_detect(
    ctx: InstallContext, tmp_path: Path
) -> None:
    payload = b"installer-bytes"
    installer = tmp_path / "setup.exe"
    installer.write_bytes(payload)
    detect = Path.home() / "Dictation" / "dictation.exe"
    ctx.runner = _WritingRunner(detect)
    component = Component(
        key="dictation-service",
        type="service",
        artifact=_artifact(
            "installer",
            url=installer.as_uri(),
            sha256=hashlib.sha256(payload).hexdigest(),
            install={
                "args": ["/VERYSILENT", "/SUPPRESSMSGBOXES"],
                "detect": {"file": "$HOME/Dictation/dictation.exe"},
            },
        ),
    )
    result = _run(get_handler("service").install(component, ctx))

    assert result.status == "installed"
    assert detect.exists()
    argv = ctx.runner.calls[0]
    assert argv[0].endswith("setup.exe")
    assert argv[1:] == ("/VERYSILENT", "/SUPPRESSMSGBOXES")


def test_service_refuses_bad_sha_before_running(ctx: InstallContext, tmp_path: Path) -> None:
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"payload")
    ctx.runner = FakeRunner()
    component = Component(
        key="dictation-service",
        type="service",
        artifact=_artifact(
            "installer",
            url=installer.as_uri(),
            sha256="1" * 64,
            install={
                "args": ["/VERYSILENT"],
                "detect": {"file": "$HOME/Dictation/dictation.exe"},
            },
        ),
    )
    with pytest.raises(ChecksumError):
        _run(get_handler("service").install(component, ctx))
    assert ctx.runner.calls == []


def test_service_is_idempotent_when_detect_exists(ctx: InstallContext) -> None:
    detect = Path.home() / "Dictation" / "dictation.exe"
    detect.parent.mkdir(parents=True)
    detect.write_text("x", encoding="utf-8")
    ctx.runner = FakeRunner()
    component = Component(
        key="dictation-service",
        type="service",
        artifact=_artifact(
            "installer",
            url="file:///nonexistent",
            sha256="2" * 64,
            install={"args": [], "detect": {"file": "$HOME/Dictation/dictation.exe"}},
        ),
    )
    result = _run(get_handler("service").install(component, ctx))
    assert result.status == "already-installed"
    assert ctx.runner.calls == []


# ── skill ───────────────────────────────────────────────────────────────────

def test_skill_delegates_to_existing_installer(ctx: InstallContext) -> None:
    called: list[str] = []

    async def _fake_installer(slug: str, context: InstallContext) -> None:
        called.append(slug)
        (context.paths.skill_store / slug).mkdir(parents=True, exist_ok=True)

    ctx.skill_installer = _fake_installer
    component = Component(
        key="atlas-skill",
        type="skill",
        artifact=_artifact("skill-ref", skill_ref="atlas"),
    )
    result = _run(get_handler("skill").install(component, ctx))

    assert result.status == "installed"
    assert called == ["atlas"]


def test_skill_is_idempotent_when_already_in_store(ctx: InstallContext) -> None:
    called: list[str] = []

    async def _fake_installer(slug: str, context: InstallContext) -> None:  # noqa: ARG001
        called.append(slug)

    ctx.skill_installer = _fake_installer
    (ctx.paths.skill_store / "atlas").mkdir(parents=True)
    component = Component(
        key="atlas-skill",
        type="skill",
        artifact=_artifact("skill-ref", skill_ref="atlas"),
    )
    result = _run(get_handler("skill").install(component, ctx))
    assert result.status == "already-installed"
    assert called == []


def test_skill_default_install_calls_existing_chain(
    ctx: InstallContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без инъекции навык ставит ровно ``_install_chain`` из ``skillery install``."""
    import skillery_cli.__main__ as main_mod
    from skillery_cli.commands import _common
    from skillery_cli.config import ClientConfig

    calls: dict = {}

    async def _fake_chain(cfg, access, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls["access"] = access
        calls.update(kwargs)
        (ctx.paths.skill_store / kwargs["slug"]).mkdir(parents=True, exist_ok=True)
        return []

    monkeypatch.setattr(main_mod, "_install_chain", _fake_chain)
    monkeypatch.setattr(_common, "get_access_token", lambda: "access-token")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: ClientConfig()))
    component = Component(
        key="atlas-skill",
        type="skill",
        artifact=_artifact("skill-ref", skill_ref="atlas"),
    )

    result = _run(get_handler("skill").install(component, ctx))

    assert result.status == "installed"
    assert calls["slug"] == "atlas"
    assert calls["channel"] == "published"
    assert calls["scope"] == "global"
    assert calls["access"] == "access-token"


def test_skill_default_remove_calls_existing_installer(
    ctx: InstallContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снятие идёт через ``SkillInstaller.remove``, а не удалением папки."""
    from skillery_cli.config import ClientConfig
    from skillery_cli.core import installer as installer_mod

    seen: dict = {}

    class _FakeInstaller:
        def __init__(self, target, store_dir) -> None:  # noqa: ANN001
            seen["target"] = target
            seen["store_dir"] = store_dir

        def remove(self, *, slug, purge=False):  # noqa: ANN001, ANN201
            seen["remove"] = (slug, purge)
            return None

    monkeypatch.setattr(installer_mod, "SkillInstaller", _FakeInstaller)
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: ClientConfig()))
    (ctx.paths.skill_store / "atlas").mkdir(parents=True)
    component = Component(
        key="atlas-skill",
        type="skill",
        artifact=_artifact("skill-ref", skill_ref="atlas"),
    )

    result = _run(get_handler("skill").remove(component, ctx))

    assert result.status == "removed"
    assert seen["remove"] == ("atlas", True)


def test_unknown_type_has_no_handler() -> None:
    with pytest.raises(LookupError):
        get_handler("daemon")
