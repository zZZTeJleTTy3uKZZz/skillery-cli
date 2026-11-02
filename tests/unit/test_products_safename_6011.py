"""#6011: ``products.json`` — недоверенные данные при снятии (safeJoin как в Go).

``bin``, ``skill_ref`` и ``install.name`` из состояния становятся именами
файлов и каталогов внутри своих корней. Файл общий с Go-CLI (и с чужими,
более новыми версиями обоих CLI), поэтому имя из него — такие же недоверенные
данные, как имя из install-plan: снятие обязано отказаться, а не выйти за
корень и не снести соседний каталог.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from skillery_cli.products.engine import remove_component
from skillery_cli.products.handlers.base import InstallContext, ProductError
from skillery_cli.products.paths import home_paths
from skillery_cli.products.runner import FakeRunner
from skillery_cli.products.safename import safe_join, validate_simple_name
from skillery_cli.products.state import InstalledState

#: Артефакт в том виде, в каком его пишет состояние: снимать нечем, если его нет.
_ARTIFACT = {
    "kind": "binary",
    "os": "linux",
    "arch": "amd64",
    "host": "",
    "url": "",
    "sha256": "",
    "size": None,
    "skill_ref": "",
    "install": {},
}


@pytest.fixture(autouse=True)
def _clean_product_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DSH_HOME", "DSH_PROFILE", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def ctx() -> InstallContext:
    return InstallContext(paths=home_paths(), runner=FakeRunner(), state=InstalledState())


def _record(
    ctx: InstallContext,
    *,
    component_type: str,
    artifact: dict[str, object],
    hosts: tuple[str, ...] = (),
) -> None:
    entry: dict[str, object] = {
        "type": component_type,
        "status": "installed",
        "artifact": artifact,
    }
    if hosts:
        entry["hosts"] = list(hosts)
    ctx.state.products["demo"] = {
        "version": "1.0.0",
        "components": {"c1": entry},
    }


def _run(coro):
    return asyncio.run(coro)


# ── сам помощник ────────────────────────────────────────────────────────────


def test_safe_join_keeps_plain_name(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "plugin-1") == tmp_path / "plugin-1"
    assert validate_simple_name("bin", "tool_2.bin") == "tool_2.bin"


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "a\\b", "/abs", "a b"])
def test_safe_join_refuses_non_simple_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ProductError):
        safe_join(tmp_path, name)


# ── снятие по данным состояния ──────────────────────────────────────────────


def test_remove_cli_refuses_bin_outside_bin_dir(ctx: InstallContext) -> None:
    victim = ctx.paths.bin_dir.parent / "victim-file"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("keep", encoding="utf-8")
    _record(
        ctx,
        component_type="cli",
        artifact={**_ARTIFACT, "install": {"bin": "../victim-file"}},
    )

    run = _run(remove_component("demo", "c1", ctx))

    assert run.status == "failed"
    assert victim.is_file(), "снятие вышло за каталог bin и удалило чужой файл"
    assert ctx.state.get_product("demo")["components"]["c1"]["status"] == "installed"


def test_remove_cli_refuses_absolute_bin(ctx: InstallContext, tmp_path: Path) -> None:
    victim = tmp_path / "victim-file"
    victim.write_text("keep", encoding="utf-8")
    _record(
        ctx,
        component_type="cli",
        artifact={**_ARTIFACT, "install": {"bin": str(victim)}},
    )

    run = _run(remove_component("demo", "c1", ctx))

    assert run.status == "failed"
    assert victim.is_file()


def test_remove_skill_refuses_slug_outside_store(ctx: InstallContext) -> None:
    victim = ctx.paths.skill_store.parent / "victim"
    victim.mkdir(parents=True, exist_ok=True)
    _record(
        ctx,
        component_type="skill",
        artifact={
            **_ARTIFACT,
            "kind": "skill-ref",
            "skill_ref": "..",
        },
    )

    run = _run(remove_component("demo", "c1", ctx))

    assert run.status == "failed"
    assert victim.is_dir(), "снятие вышло за стор навыков и удалило чужой каталог"


def test_remove_plugin_refuses_name_outside_plugins(ctx: InstallContext) -> None:
    """Харнес есть на устройстве — иначе проверка не дошла бы до пути."""
    victim = ctx.paths.dsh_plugins.parent / "victim"
    victim.mkdir(parents=True, exist_ok=True)
    _record(
        ctx,
        component_type="plugin",
        artifact={**_ARTIFACT, "kind": "archive", "install": {"name": ".."}},
        hosts=("dsh",),
    )

    run = _run(remove_component("demo", "c1", ctx))

    assert run.status == "failed"
    assert victim.is_dir(), "снятие вышло за каталог плагинов DSH"


def test_remove_claude_plugin_refuses_name_outside_plugins(ctx: InstallContext) -> None:
    victim = ctx.paths.claude_plugins.parent / "victim"
    victim.mkdir(parents=True, exist_ok=True)
    _record(
        ctx,
        component_type="plugin",
        artifact={**_ARTIFACT, "kind": "archive", "install": {"name": "../victim"}},
        hosts=("claude-code",),
    )

    run = _run(remove_component("demo", "c1", ctx))

    assert run.status == "failed"
    assert victim.is_dir(), "снятие вышло за каталог плагинов Claude Code"
