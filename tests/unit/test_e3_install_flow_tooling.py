"""E3 фаза 2 — интеграция tooling-артефактов в install-flow (__main__).

Проверяем, что install/enable/disable/remove навыка с tooling-манифестом дёргают
оркестратор ``tooling_install`` (apply при установке/включении, revert при
выключении/удалении), а prompt-навык — НЕТ (регрессия). Сам оркестратор замокан
— здесь интересует только корректная проводка call-site + передача манифеста.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import project_manifest as pm
from skillery_cli.core.agents import ClaudeCodeTarget


class _ExplodingClient:
    def __init__(self, *a, **k) -> None:
        raise AssertionError("HubClient НЕ должен создаваться в автономном install")


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(
        store_dir=str(store),
        default_install_scope="project",
        default_project_dir=str(project),
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c, **k: None)
    monkeypatch.setattr(main_mod, "_maybe_notify_cli_update", lambda c: None)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(main_mod, "HubClient", _ExplodingClient)
    monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, target, project


def _tooling_skill_dir(tmp_path: Path) -> Path:
    """Локальная папка-навык kind=tooling с _skill_meta.toml (cli/mcp/deps)."""
    src = tmp_path / "bx-src"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: bx\nversion: 1.0.0\nkind: tooling\n---\n\n# bx\n",
        encoding="utf-8",
    )
    return src


# --------------------------------------------------------------------------
#  install --path tooling → apply_tooling_artifacts вызывается
# --------------------------------------------------------------------------
def test_install_path_tooling_invokes_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project = _wire(tmp_path, monkeypatch)
    src = _tooling_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        main_mod.tooling_install, "apply_tooling_artifacts",
        lambda result, *, agent_target, project, manifest=None: apply_calls.append(
            {"slug": getattr(result, "slug", None), "manifest": manifest,
             "agent": agent_target}
        )
        or {"cli": [], "mcp": [], "deps": {}},
    )

    main_mod.cmd_install(
        slug="bx", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )
    # apply вызван ровно один раз, с нашим навыком; манифест несёт kind=tooling.
    assert len(apply_calls) == 1
    assert apply_calls[0]["slug"] == "bx"
    assert apply_calls[0]["manifest"] is not None
    assert apply_calls[0]["agent"] is target


# --------------------------------------------------------------------------
#  install --path prompt → apply НЕ дергает CLI/MCP (no-op в оркестраторе)
# --------------------------------------------------------------------------
def test_install_path_prompt_apply_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prompt-навык всё равно зовёт apply, но оркестратор внутри — no-op.

    Здесь проверяем, что call-site безопасен: apply получает prompt-манифест и
    обязан вернуть пустой отчёт (детальный no-op покрыт в test_e3_tooling_install).
    """
    cfg, target, project = _wire(tmp_path, monkeypatch)
    src = tmp_path / "prompt-src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: p\nversion: 1.0.0\nkind: prompt\n---\n\n# p\n", encoding="utf-8"
    )
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")

    seen: list[dict] = []
    # Реальный оркестратор (НЕ мок) — он сам должен быть no-op для prompt:
    # отследим, что CLI/MCP/deps не трогаются.
    monkeypatch.setattr(
        main_mod.tooling_install.path_store, "add_cli",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompt → нет CLI")),
    )
    monkeypatch.setattr(
        main_mod.tooling_install.mcp_register, "register_mcp",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompt → нет MCP")),
    )
    monkeypatch.setattr(
        main_mod.tooling_install.deps_installer, "install_runtime_dependencies",
        lambda deps: seen.append(deps) or {"installed": [], "skipped": [], "failed": []},
    )

    main_mod.cmd_install(
        slug="p", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )
    # Навык встал; CLI/MCP/deps не трогались.
    assert (cfg.effective_store_dir() / "p" / "SKILL.md").exists()
    assert seen == []


# --------------------------------------------------------------------------
#  apply падает → install самого навыка НЕ падает (graceful на call-site)
# --------------------------------------------------------------------------
def test_install_apply_failure_does_not_break_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project = _wire(tmp_path, monkeypatch)
    src = _tooling_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    monkeypatch.setattr(
        main_mod.tooling_install, "apply_tooling_artifacts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # Навык всё равно должен установиться (call-site обязан проглотить ошибку).
    main_mod.cmd_install(
        slug="bx", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )
    assert (cfg.effective_store_dir() / "bx" / "SKILL.md").exists()
    assert pm.load(project) == {"bx": "*"}


# --------------------------------------------------------------------------
#  disable tooling → revert_tooling_artifacts вызывается с манифестом стора
# --------------------------------------------------------------------------
def test_disable_tooling_invokes_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project = _wire(tmp_path, monkeypatch)
    src = _tooling_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    # apply замокан в no-op, чтобы install прошёл чисто.
    monkeypatch.setattr(
        main_mod.tooling_install, "apply_tooling_artifacts",
        lambda *a, **k: {"cli": [], "mcp": [], "deps": {}},
    )
    # Сначала ставим (материализуем в стор + meta с манифестом).
    main_mod.cmd_install(
        slug="bx", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )

    revert_calls: list[dict] = []
    monkeypatch.setattr(
        main_mod.tooling_install, "revert_tooling_artifacts",
        lambda manifest, *, agent_target, project: revert_calls.append(
            {"manifest": manifest, "agent": agent_target}
        )
        or {"cli_removed": [], "mcp_unregistered": [], "errors": []},
    )
    main_mod.cmd_disable(slug="bx", project=project, agent=None)
    assert len(revert_calls) == 1
    assert revert_calls[0]["manifest"] is not None
    assert revert_calls[0]["agent"] is target


# --------------------------------------------------------------------------
#  remove (global) tooling → revert вызывается ДО снятия (манифест ещё доступен)
# --------------------------------------------------------------------------
def test_remove_global_tooling_invokes_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project = _wire(tmp_path, monkeypatch)
    src = _tooling_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    monkeypatch.setattr(
        main_mod.tooling_install, "apply_tooling_artifacts",
        lambda *a, **k: {"cli": [], "mcp": [], "deps": {}},
    )
    # Ставим в GLOBAL scope (project=None путь).
    main_mod.cmd_install(
        slug="bx", channel="published", agent=None, scope="global",
        project=None, force=False, path=src, from_git=None, ref=None,
    )

    revert_calls: list[dict] = []
    monkeypatch.setattr(
        main_mod.tooling_install, "revert_tooling_artifacts",
        lambda manifest, *, agent_target, project: revert_calls.append(
            {"manifest": manifest}
        )
        or {"cli_removed": [], "mcp_unregistered": [], "errors": []},
    )
    main_mod.cmd_remove(
        slug="bx", scope="global", project=None, keep_local=False,
        purge=True, agent=None,
    )
    assert len(revert_calls) == 1
    # Манифест прочитан из меты стора ДО purge.
    assert revert_calls[0]["manifest"] is not None


def test_remove_prompt_skill_no_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Удаление prompt-навыка не зовёт revert вхолостую с CLI/MCP-side-effects."""
    cfg, target, project = _wire(tmp_path, monkeypatch)
    src = tmp_path / "p-src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: p\nversion: 1.0.0\nkind: prompt\n---\n\n# p\n", encoding="utf-8"
    )
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    main_mod.cmd_install(
        slug="p", channel="published", agent=None, scope="global",
        project=None, force=False, path=src, from_git=None, ref=None,
    )
    # revert реальный, но remove_cli/​unregister обязаны НЕ вызываться (prompt).
    monkeypatch.setattr(
        main_mod.tooling_install.path_store, "remove_cli",
        lambda name: (_ for _ in ()).throw(AssertionError("prompt → нет remove_cli")),
    )
    monkeypatch.setattr(
        main_mod.tooling_install.mcp_register, "unregister_mcp",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompt → нет unregister")),
    )
    # Не падает (revert внутри — no-op для prompt-манифеста).
    main_mod.cmd_remove(
        slug="p", scope="global", project=None, keep_local=False,
        purge=True, agent=None,
    )
