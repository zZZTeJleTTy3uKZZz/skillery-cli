"""E3 фаза 2 — core/tooling_install: оркестрация CLI/MCP/deps при установке навыка.

После материализации навыка с ``kind=tooling`` (или просто несущего cli/mcp/
runtime_dependencies) install-flow зовёт ``apply_tooling_artifacts``:
- ``runtime_dependencies`` → deps_installer;
- каждая ``[[cli]]`` → path_store.add_cli (+ ensure_on_path один раз);
- каждый ``[[mcp]]`` → mcp_register.register_mcp.

При disable/remove навыка — ``revert_tooling_artifacts``: снимает CLI (по
command_name) и MCP (по server_name). ВСЁ опционально и graceful: исключение в
любой ветке НЕ ломает установку самого навыка.

Здесь мокаются path_store / mcp_register / deps_installer — проверяем именно
оркестрацию (что зовётся, с чем, и что не зовётся для prompt-навыка).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core import tooling_install as ti


class _FakeResult:
    """Подобие installer.InstallResult: несёт slug/skill_id/store_dir/manifest."""

    def __init__(self, *, slug=None, skill_id=None, store_dir=None, manifest=None):
        self.slug = slug
        self.skill_id = skill_id
        self.store_dir = store_dir
        self.manifest = manifest or {}


class _Target:
    name = "claude_code"
    dirname = ".claude"


def _tooling_manifest() -> dict:
    return {
        "version": "1.0.0",
        "kind": "tooling",
        "cli": [{"command_name": "bx", "entrypoint": "bx_cli.main:run"}],
        "mcp": [
            {"server_name": "bx-mcp", "transport": "stdio",
             "config": {"command": "bx-mcp"}}
        ],
        "runtime_dependencies": [{"kind": "pip", "spec": "httpx>=0.27"}],
    }


# --------------------------------------------------------------------------
#  apply: tooling-навык → CLI + MCP + deps
# --------------------------------------------------------------------------
def test_apply_tooling_installs_cli_mcp_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    added_cli: list[tuple] = []
    mcp_calls: list[dict] = []
    dep_calls: list[list] = []
    ensure_calls: list[bool] = []

    monkeypatch.setattr(
        ti.path_store, "add_cli",
        lambda cmd, ep, *, skill_slug, **kw: added_cli.append((cmd, ep, skill_slug))
        or Path(f"/bin/{cmd}"),
    )
    monkeypatch.setattr(
        ti.path_store, "ensure_on_path",
        lambda: ensure_calls.append(True) or {"status": "already", "bin_dir": "/bin"},
    )
    monkeypatch.setattr(
        ti.mcp_register, "register_mcp",
        lambda server, *, agent_target, project, **kw: mcp_calls.append(server)
        or {"status": "registered", "server_name": server["server_name"]},
    )
    monkeypatch.setattr(
        ti.deps_installer, "install_runtime_dependencies",
        lambda deps, *, python_executable=None: dep_calls.append(list(deps))
        or {"installed": list(deps), "skipped": [], "failed": []},
    )

    result = _FakeResult(slug="bitrix24", store_dir=Path("/store/bitrix24"),
                         manifest=_tooling_manifest())
    report = ti.apply_tooling_artifacts(result, agent_target=_Target(), project=None)

    # CLI поставлен (с привязкой к навыку), ensure_on_path вызван РОВНО один раз.
    assert added_cli and added_cli[0][0] == "bx"
    assert added_cli[0][2] == "bitrix24"
    assert ensure_calls == [True]
    # MCP зарегистрирован.
    assert mcp_calls and mcp_calls[0]["server_name"] == "bx-mcp"
    # deps поставлены.
    assert dep_calls == [[{"kind": "pip", "spec": "httpx>=0.27"}]]
    # отчёт несёт все три секции.
    assert report["cli"] and report["mcp"] and report["deps"]


def test_apply_ensure_on_path_called_once_for_many_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_calls: list[bool] = []
    monkeypatch.setattr(ti.path_store, "add_cli",
                        lambda cmd, ep, *, skill_slug, **kw: Path(f"/bin/{cmd}"))
    monkeypatch.setattr(ti.path_store, "ensure_on_path",
                        lambda: ensure_calls.append(True) or {"status": "added", "bin_dir": "/b"})
    monkeypatch.setattr(ti.mcp_register, "register_mcp",
                        lambda *a, **k: {"status": "registered"})
    monkeypatch.setattr(ti.deps_installer, "install_runtime_dependencies",
                        lambda deps, *, python_executable=None: {
                            "installed": [], "skipped": [], "failed": []})
    manifest = {
        "kind": "tooling",
        "cli": [
            {"command_name": "a", "entrypoint": "p.a:run"},
            {"command_name": "b", "entrypoint": "p.b:run"},
        ],
    }
    result = _FakeResult(slug="s", store_dir=Path("/store/s"), manifest=manifest)
    ti.apply_tooling_artifacts(result, agent_target=_Target(), project=None)
    # 2 CLI, но ensure_on_path — один раз.
    assert ensure_calls == [True]


# --------------------------------------------------------------------------
#  apply: prompt-навык → НИЧЕГО не трогаем (регрессия)
# --------------------------------------------------------------------------
def test_apply_prompt_skill_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):
        raise AssertionError("prompt-навык не должен трогать CLI/MCP/deps")

    monkeypatch.setattr(ti.path_store, "add_cli", _boom)
    monkeypatch.setattr(ti.path_store, "ensure_on_path", _boom)
    monkeypatch.setattr(ti.mcp_register, "register_mcp", _boom)
    monkeypatch.setattr(ti.deps_installer, "install_runtime_dependencies", _boom)

    manifest = {"version": "1.0.0", "kind": "prompt"}  # нет cli/mcp/deps
    result = _FakeResult(slug="prompt-skill", manifest=manifest)
    report = ti.apply_tooling_artifacts(result, agent_target=_Target(), project=None)
    assert report["cli"] == []
    assert report["mcp"] == []
    assert report["deps"] == {} or report["deps"].get("installed") == []


def test_apply_no_manifest_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Навык без manifest (или с пустым) → no-op, не падаем."""
    monkeypatch.setattr(ti.path_store, "add_cli",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("noop")))
    result = _FakeResult(slug="x", manifest={})
    report = ti.apply_tooling_artifacts(result, agent_target=_Target(), project=None)
    assert report["cli"] == [] and report["mcp"] == []


# --------------------------------------------------------------------------
#  graceful: ошибка в CLI/MCP/deps НЕ ломает install
# --------------------------------------------------------------------------
def test_apply_cli_error_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ti.path_store, "add_cli",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(ti.path_store, "ensure_on_path",
                        lambda: {"status": "already", "bin_dir": "/b"})
    monkeypatch.setattr(ti.mcp_register, "register_mcp",
                        lambda *a, **k: {"status": "registered"})
    monkeypatch.setattr(ti.deps_installer, "install_runtime_dependencies",
                        lambda deps, *, python_executable=None: {
                            "installed": [], "skipped": [], "failed": []})
    result = _FakeResult(slug="s", store_dir=Path("/store/s"),
                         manifest=_tooling_manifest())
    # Не бросает; CLI-ветка зафиксировала ошибку.
    report = ti.apply_tooling_artifacts(result, agent_target=_Target(), project=None)
    assert any(c.get("status") == "error" for c in report["cli"])


def test_apply_deps_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ti.path_store, "add_cli",
                        lambda cmd, ep, *, skill_slug, **kw: Path(f"/bin/{cmd}"))
    monkeypatch.setattr(ti.path_store, "ensure_on_path",
                        lambda: {"status": "already", "bin_dir": "/b"})
    monkeypatch.setattr(ti.mcp_register, "register_mcp",
                        lambda *a, **k: {"status": "registered"})
    monkeypatch.setattr(
        ti.deps_installer, "install_runtime_dependencies",
        lambda deps, *, python_executable=None: (_ for _ in ()).throw(
            RuntimeError("pip exploded")
        ),
    )
    result = _FakeResult(slug="s", store_dir=Path("/store/s"),
                         manifest=_tooling_manifest())
    # Падение deps не валит общий флоу.
    report = ti.apply_tooling_artifacts(result, agent_target=_Target(), project=None)
    assert "deps" in report  # отчёт есть, install продолжился


# --------------------------------------------------------------------------
#  entrypoint resolution
# --------------------------------------------------------------------------
def test_entrypoint_module_callable_becomes_python_command() -> None:
    """``module:callable`` → исполняемая python-команда (не как есть)."""
    cmd = ti._entrypoint_to_command("bx_cli.main:run", store_dir=Path("/store/bx"))
    assert cmd is not None
    assert "bx_cli.main" in cmd
    assert "run" in cmd
    # вызывается текущим интерпретатором python.
    assert "-c" in cmd or "-m" in cmd


def test_entrypoint_plain_command_passthrough() -> None:
    cmd = ti._entrypoint_to_command("python -m bx_cli", store_dir=Path("/store/bx"))
    assert cmd == "python -m bx_cli"


def test_entrypoint_none_skips_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """entrypoint=None → CLI пропускается со status=skipped, без падения."""
    add_calls: list = []
    monkeypatch.setattr(ti.path_store, "add_cli",
                        lambda *a, **k: add_calls.append(a) or Path("/bin/x"))
    monkeypatch.setattr(ti.path_store, "ensure_on_path",
                        lambda: {"status": "already", "bin_dir": "/b"})
    manifest = {"kind": "tooling", "cli": [{"command_name": "x", "entrypoint": None}]}
    result = _FakeResult(slug="s", store_dir=Path("/store/s"), manifest=manifest)
    report = ti.apply_tooling_artifacts(result, agent_target=_Target(), project=None)
    assert add_calls == []  # add_cli НЕ звался
    assert report["cli"] and report["cli"][0]["status"] == "skipped"


# --------------------------------------------------------------------------
#  revert: снять CLI + MCP по манифесту
# --------------------------------------------------------------------------
def test_revert_removes_cli_and_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    removed_cli: list[str] = []
    unreg_mcp: list[str] = []
    monkeypatch.setattr(ti.path_store, "remove_cli",
                        lambda name, **kw: removed_cli.append(name) or True)
    monkeypatch.setattr(
        ti.mcp_register, "unregister_mcp",
        lambda name, *, agent_target, project, **kw: unreg_mcp.append(name)
        or {"status": "unregistered"},
    )
    report = ti.revert_tooling_artifacts(
        _tooling_manifest(), agent_target=_Target(), project=None
    )
    assert removed_cli == ["bx"]
    assert unreg_mcp == ["bx-mcp"]
    assert report["cli_removed"] == ["bx"]
    assert report["mcp_unregistered"] == ["bx-mcp"]


def test_revert_prompt_skill_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ti.path_store, "remove_cli",
                        lambda name, **kw: (_ for _ in ()).throw(AssertionError("noop")))
    report = ti.revert_tooling_artifacts(
        {"kind": "prompt"}, agent_target=_Target(), project=None
    )
    assert report["cli_removed"] == []
    assert report["mcp_unregistered"] == []


def test_revert_error_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ti.path_store, "remove_cli",
                        lambda name, **kw: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(ti.mcp_register, "unregister_mcp",
                        lambda *a, **k: {"status": "unregistered"})
    # Не бросает даже если remove_cli падает.
    report = ti.revert_tooling_artifacts(
        _tooling_manifest(), agent_target=_Target(), project=None
    )
    assert "cli_removed" in report
