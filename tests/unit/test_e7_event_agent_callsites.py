"""E7 аналитика — каждая track-точка несёт `agent` (target.name).

Зеркалит ``test_an_event_split`` (source-ось), но проверяет, что во ВСЕХ
жизненных событиях (install/update/enable/disable/uninstall) в payload уходит
``agent`` — имя таргета-агента (``target.name``), куда CLI ставит/линкует навык.
Так дашборд узнаёт, КАКОЙ ИИ-агент адоптит навыки.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import CodexTarget


def _events_of(events: list, etype: str) -> list:
    return [(a, k) for (a, k) in events if a and a[0] == etype]


def _wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    logged_in: bool = True,
) -> tuple[ClientConfig, CodexTarget, Path, list]:
    # CodexTarget (name="codex") — чтобы отличать от дефолтного claude_code и
    # доказать, что в payload идёт ИМЕННО target.name, а не хардкод.
    target = CodexTarget(root=tmp_path / ".codex")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(
        store_dir=str(store),
        default_install_scope="project",
        default_project_dir=str(project),
    )
    if logged_in:
        cfg.user_email = "x@y.io"
        cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c, **k: None)
    monkeypatch.setattr(main_mod, "_maybe_notify_cli_update", lambda c: None)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(out_mod, "_mode", "json")
    events: list = []
    monkeypatch.setattr(
        main_mod, "track_skill_event", lambda *a, **k: events.append((a, k))
    )
    return cfg, target, project, events


def _make_skill_dir(tmp_path: Path, *, version: str = "1.2.3") -> Path:
    src = tmp_path / "skill-src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(
        f"---\nname: demo\nversion: {version}\n---\n\n# demo\n", encoding="utf-8"
    )
    return src


def _assert_agent_all(events: list, agent: str = "codex") -> None:
    """Каждый трекнутый lifecycle-event несёт payload['agent']==agent."""
    assert events, "ожидались track-события"
    for a, k in events:
        assert k.get("agent") == agent, f"{a[0]} без agent={agent}: {k}"


# ========== install --path (global) ==========
def test_install_path_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch, logged_in=False)
    src = _make_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    main_mod.cmd_install(
        slug="demo", channel="published", agent=None, scope="global",
        project=None, force=False, path=src, from_git=None, ref=None,
    )
    _assert_agent_all(_events_of(events, "skill.install"))


# ========== install --path (project) → install + enable ==========
def test_install_path_project_carries_agent_on_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch, logged_in=False)
    src = _make_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    main_mod.cmd_install(
        slug="demo", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )
    _assert_agent_all(_events_of(events, "skill.install"))
    _assert_agent_all(_events_of(events, "skill.enable"))


# ========== install hub (mocked chain) → install + enable ==========
def test_install_hub_project_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug, *a, **k):
            return {
                "skill_slug": slug, "skill_id": "7", "version": "1.0.0",
                "commit_sha": "abc", "repo_url": None,
                "manifest": {"version": "1.0.0", "files": []},
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)
    main_mod.cmd_install(
        slug="bitrix24", channel="published", agent=None, scope="project",
        project=project, force=False, path=None, from_git=None, ref=None,
    )
    _assert_agent_all(_events_of(events, "skill.install"))
    _assert_agent_all(_events_of(events, "skill.enable"))


# ========== enable (store-first re-link) ==========
def test_enable_store_first_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    installer = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path)
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=None, force=False)
    main_mod.cmd_enable(slug="demo", project=project, agent=None,
                        force=False, channel="published")
    _assert_agent_all(_events_of(events, "skill.enable"))


# ========== sync (массовый re-link) ==========
def test_sync_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    installer = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path)
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=None, force=False)
    from skillery_cli.core import project_manifest as pm
    pm.add(project, "demo")
    main_mod.cmd_sync(project=project, prune=False, agent=None,
                      channel="published")
    _assert_agent_all(_events_of(events, "skill.enable"))


# ========== disable ==========
def test_disable_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    installer = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path)
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=project, force=False)
    main_mod.cmd_disable(slug="demo", project=project, agent=None)
    _assert_agent_all(_events_of(events, "skill.disable"))


# ========== remove --scope project (без purge) → disable ==========
def test_remove_project_no_purge_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    installer = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path)
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=project, force=False)
    main_mod.cmd_remove(slug="demo", scope="project", project=project,
                        keep_local=False, purge=False, agent=None)
    _assert_agent_all(_events_of(events, "skill.disable"))


# ========== remove --purge → uninstall ==========
def test_remove_purge_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    installer = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path)
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=project, force=False)
    main_mod.cmd_remove(slug="demo", scope="project", project=project,
                        keep_local=False, purge=True, agent=None)
    _assert_agent_all(_events_of(events, "skill.uninstall"))


# ========== update (mocked bundle + installer) → skill.update ==========
def test_update_carries_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    # Реальный installer кладёт навык в стор (local-path, без repo_url).
    real = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path, version="1.0.0")
    real.install(slug="demo", version="1.0.0", commit_sha="old",
                 repo_url=None, local_src=src,
                 manifest={"version": "1.0.0", "files": []},
                 project=None, force=False)

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug, *a, **k):
            return {
                "skill_slug": slug, "skill_id": "7", "version": "2.0.0",
                "commit_sha": "new", "repo_url": None,
                "manifest": {"version": "2.0.0", "files": []},
            }

        async def close(self):
            return None

    # Тонкий installer-stub: install() для update-ветки возвращает is_update,
    # не лезет в git (фокус теста — agent в payload, не механика installer'а).
    from skillery_cli.core.installer import InstallResult

    class _FakeInstaller:
        def __init__(self, *a, **k):
            pass

        def install(self, *, slug=None, version="2.0.0", **k):
            return InstallResult(
                slug=slug, target_dir=target.slug_dir(slug or "demo"),
                version=version, is_update=True, scope="global",
                update_diff={"added": 0, "changed": 1, "removed": 0},
            )

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)
    monkeypatch.setattr(main_mod, "SkillInstaller", _FakeInstaller)
    main_mod.cmd_update(slug="demo", all_=False, channel="published",
                        project=None, scope="global")
    _assert_agent_all(_events_of(events, "skill.update"))
