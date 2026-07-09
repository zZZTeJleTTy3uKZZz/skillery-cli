"""Аналитика-эпик W1a: разведение событий жизненного цикла + `source`.

Канон (план 2026-06-11 «Событийная модель»):
- ``skill.install`` — материализация в стор (is_update=False), source из контекста;
- ``skill.update`` — обновление контента в сторе (is_update=True);
- ``skill.enable`` — создана PROJECT-ссылка (включён в проект), scope=project;
- ``skill.disable`` — PROJECT-ссылка снята (стор цел), scope=project;
- ``skill.uninstall`` — навык удалён из стора (--purge / global remove).

`install <slug> --scope project` шлёт ДВА события: skill.install (если новый в
сторе) + skill.enable (за project-линк). `enable` (уже в сторе) → только enable.
`sync` (массовый re-link) → skill.enable на каждый. `source` (hub/local-path/
git-url) кладётся в payload каждого install/enable/update.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget


def _events_of(events: list, etype: str) -> list:
    """Все (args, kwargs) вызовы track_skill_event с данным event_type."""
    return [(a, k) for (a, k) in events if a and a[0] == etype]


def _wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    logged_in: bool = True,
) -> tuple[ClientConfig, ClaudeCodeTarget, Path, list]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
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
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c: None)
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


# ========== install --path → skill.install(source=local-path) ==========
def test_install_path_global_emits_install_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch, logged_in=False)
    src = _make_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")

    main_mod.cmd_install(
        slug="demo", channel="published", agent=None, scope="global",
        project=None, force=False, path=src, from_git=None, ref=None,
    )
    installs = _events_of(events, "skill.install")
    assert len(installs) == 1
    assert installs[0][1]["scope"] == "global"
    assert installs[0][1]["source"] == "local-path"
    # global → НЕ project-линк → enable НЕ шлём
    assert _events_of(events, "skill.enable") == []


def test_install_path_project_emits_install_and_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--scope project → skill.install (стор) + skill.enable (project-линк)."""
    cfg, target, project, events = _wire(tmp_path, monkeypatch, logged_in=False)
    src = _make_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")

    main_mod.cmd_install(
        slug="demo", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )
    installs = _events_of(events, "skill.install")
    enables = _events_of(events, "skill.enable")
    assert len(installs) == 1
    assert installs[0][1]["source"] == "local-path"
    assert len(enables) == 1
    assert enables[0][1]["scope"] == "project"
    assert enables[0][1]["source"] == "local-path"


def test_install_from_git_project_emits_install_and_enable_git_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        pytest.skip("git не установлен")
    cfg, target, project, events = _wire(tmp_path, monkeypatch, logged_in=False)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")

    def _g(cwd: Path, *args: str) -> None:
        subprocess.run([git, *args], cwd=str(cwd), check=True,
                       capture_output=True, text=True)

    work = tmp_path / "work"
    work.mkdir()
    _g(work, "init", "-q", "-b", "main")
    _g(work, "config", "user.email", "t@t.io")
    _g(work, "config", "user.name", "t")
    (work / "SKILL.md").write_text(
        "---\nname: g\nversion: 9.9.9\n---\n\n# g\n", encoding="utf-8"
    )
    _g(work, "add", "-A")
    _g(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    _g(tmp_path, "clone", "-q", "--bare", str(work), str(bare))

    main_mod.cmd_install(
        slug="g", channel="published", agent=None, scope="project",
        project=project, force=False, path=None, from_git=bare.as_uri(), ref=None,
    )
    installs = _events_of(events, "skill.install")
    enables = _events_of(events, "skill.enable")
    assert len(installs) == 1
    assert installs[0][1]["source"] == "git-url"
    assert len(enables) == 1
    assert enables[0][1]["source"] == "git-url"


# ========== install hub (mocked chain) → install + enable(source=hub) ==========
def test_install_hub_project_emits_install_and_enable_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug, *a, **k):
            return {
                "skill_slug": slug,
                "skill_id": "7",
                "version": "1.0.0",
                "commit_sha": "abc",
                "repo_url": None,
                "manifest": {"version": "1.0.0", "files": []},
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)

    main_mod.cmd_install(
        slug="bitrix24", channel="published", agent=None, scope="project",
        project=project, force=False, path=None, from_git=None, ref=None,
    )
    installs = _events_of(events, "skill.install")
    enables = _events_of(events, "skill.enable")
    assert len(installs) == 1
    assert installs[0][1]["source"] == "hub"
    assert installs[0][1]["scope"] == "project"
    assert len(enables) == 1
    assert enables[0][1]["source"] == "hub"
    assert enables[0][1]["scope"] == "project"


def test_install_hub_global_emits_install_no_enable(
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
        slug="bitrix24", channel="published", agent=None, scope="global",
        project=None, force=False, path=None, from_git=None, ref=None,
    )
    assert len(_events_of(events, "skill.install")) == 1
    assert _events_of(events, "skill.enable") == []


# ========== enable (store-first re-link) → skill.enable(source from meta) ==========
def test_enable_store_first_emits_enable_with_meta_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Навык уже в сторе (source=git-url в meta) → cmd_enable шлёт
    skill.enable, source читается из _skill_meta.json (НЕ skill.install)."""
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    # материализуем навык в стор «как git-url» через installer
    installer = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path)
    # install global сначала кладёт в стор; затем подменим meta.source
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=None, force=False)
    from skillery_cli.core.installer import read_meta, write_meta
    store_dir = cfg.effective_store_dir() / "demo"
    meta = read_meta(store_dir)
    meta["source"] = "git-url"
    write_meta(store_dir, meta)

    main_mod.cmd_enable(slug="demo", project=project, agent=None,
                        force=False, channel="published")

    enables = _events_of(events, "skill.enable")
    assert len(enables) == 1
    assert enables[0][1]["scope"] == "project"
    assert enables[0][1]["source"] == "git-url"
    # store-first re-link НЕ материализует заново → нет skill.install
    assert _events_of(events, "skill.install") == []


# ========== sync (массовый re-link) → skill.enable на каждый ==========
def test_sync_emits_enable_per_relink(
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

    enables = _events_of(events, "skill.enable")
    assert len(enables) == 1
    assert enables[0][1]["slug"] == "demo"
    assert enables[0][1]["scope"] == "project"
    # source из meta (local-path для install --path)
    assert enables[0][1]["source"] == "local-path"


# ========== disable → skill.disable (НЕ skill.uninstall) ==========
def test_disable_emits_skill_disable(
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

    disables = _events_of(events, "skill.disable")
    assert len(disables) == 1
    assert disables[0][1]["scope"] == "project"
    # disable снимает ссылку, стор цел → НЕ uninstall
    assert _events_of(events, "skill.uninstall") == []


# ========== remove --scope project (без purge) → skill.disable ==========
def test_remove_project_no_purge_emits_disable(
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

    assert len(_events_of(events, "skill.disable")) == 1
    assert _events_of(events, "skill.uninstall") == []


# ========== remove --purge → skill.uninstall ==========
def test_remove_purge_emits_uninstall(
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

    assert len(_events_of(events, "skill.uninstall")) == 1
    assert _events_of(events, "skill.disable") == []


# ========== remove --scope global → skill.uninstall ==========
def test_remove_global_emits_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project, events = _wire(tmp_path, monkeypatch)
    installer = main_mod.SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path)
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=None, force=False)

    main_mod.cmd_remove(slug="demo", scope="global", project=None,
                        keep_local=False, purge=False, agent=None)

    assert len(_events_of(events, "skill.uninstall")) == 1
    assert _events_of(events, "skill.disable") == []
