"""Фикс 5 (MINOR×4): манифест-симметрия и UX.

(а) cmd_remove в project-scope убирает slug из .skills-hub/skills.toml
    (как disable) + manifest_removed в JSON-ответе;
(б) cmd_migrate (scope=project) дописывает мигрированные slug'и в манифест —
    иначе следующий sync --prune снимает мигрированный навык (живой факт);
(в) hub-install stub в свежее место → warning в stderr (json-режим) +
    поле "content":"stub" в JSON-результате cmd_install;
(г) from-git: версия в _skill_meta берётся из frontmatter SKILL.md клона
    (как у --path), а не хардкод '0.0.0-local' (живой факт l2).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import skills_hub_cli.__main__ as main_mod
from skills_hub_cli import output as out_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core import linker, project_manifest as pm
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller, read_meta, write_meta

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, logged_in: bool = False):
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(store_dir=str(store))
    if logged_in:
        cfg.user_email = "x@y.io"
        cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c: None)
    monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, target, store, project


# ------------------------------------------------------------------
#  (а) remove ↔ манифест
# ------------------------------------------------------------------
def test_remove_project_scope_removes_manifest_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _cfg, target, store, project = _wire(tmp_path, monkeypatch)
    inst = SkillInstaller(target, store_dir=store)
    inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                 repo_url=None, manifest=_MANIFEST, project=project)
    pm.add(project, "demo")

    main_mod.cmd_remove(slug="demo", scope="project", project=project,
                        keep_local=False, purge=False, agent=None)

    assert pm.load(project) == {}  # как disable
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["removed"] is True
    assert payload["manifest_removed"] is True


def test_remove_global_scope_does_not_touch_project_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _cfg, target, store, project = _wire(tmp_path, monkeypatch)
    inst = SkillInstaller(target, store_dir=store)
    inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                 repo_url=None, manifest=_MANIFEST)  # global
    pm.add(project, "demo")  # манифест проекта — про project scope

    main_mod.cmd_remove(slug="demo", scope="global", project=None,
                        keep_local=False, purge=False, agent=None)

    assert pm.load(project) == {"demo": "*"}  # не тронут
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["manifest_removed"] is False


# ------------------------------------------------------------------
#  (б) migrate ↔ манифест
# ------------------------------------------------------------------
def test_migrate_project_scope_adds_migrated_to_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _cfg, target, _store, project = _wire(tmp_path, monkeypatch)
    # Старая copy-установка в project scope (meta есть, ссылки нет).
    slug_dir = target.slug_dir("legacy", project=project)
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("# legacy\n", encoding="utf-8")
    write_meta(slug_dir, {"slug": "legacy", "version": "1.0.0", "manifest": {}})

    main_mod.cmd_migrate(scope="project", project=project, dry_run=False, agent=None)

    # Мигрирован: copy → стор + ссылка.
    assert linker.is_link(target.slug_dir("legacy", project=project))
    # И дописан в манифест — иначе sync --prune его снимет.
    assert pm.load(project) == {"legacy": "*"}
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["manifest_added"] == ["legacy"]


def test_migrate_dry_run_does_not_touch_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _cfg, target, _store, project = _wire(tmp_path, monkeypatch)
    slug_dir = target.slug_dir("legacy", project=project)
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("# legacy\n", encoding="utf-8")
    write_meta(slug_dir, {"slug": "legacy", "version": "1.0.0", "manifest": {}})

    main_mod.cmd_migrate(scope="project", project=project, dry_run=True, agent=None)

    assert pm.load(project) == {}
    assert not linker.is_link(slug_dir)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["manifest_added"] == []


# ------------------------------------------------------------------
#  (в) hub-install stub: warning + "content":"stub"
# ------------------------------------------------------------------
def test_install_hub_stub_fresh_flags_content_and_warns_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cfg, _target, store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            return {
                "skill_slug": "stubby",
                "skill_id": 5,
                "version": "1.0.0",
                "commit_sha": "c0ffee",
                "repo_url": None,  # ← у скилла в хабе нет git-репо
                "manifest": {"version": "1.0.0", "files": []},
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)

    main_mod.cmd_install(
        slug="stubby", channel="published", agent=None, scope="project",
        project=project, force=False, path=None, from_git=None, ref=None,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload[0]["content"] == "stub"
    # Warning ушёл в stderr (stdout остаётся чистым JSON).
    assert "stub" in captured.err.lower()
    # Stub реально материализован (свежее место — разрешено).
    assert (store / "stubby" / "SKILL.md").exists()


# ------------------------------------------------------------------
#  (г) from-git: версия из frontmatter клона
# ------------------------------------------------------------------
def _make_bare_repo(tmp_path: Path, name: str, version: str) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git не установлен")

    def _g(cwd: Path, *args: str) -> None:
        subprocess.run([git, *args], cwd=str(cwd), check=True,
                       capture_output=True, text=True)

    work = tmp_path / f"work-{name}"
    work.mkdir()
    _g(work, "init", "-q", "-b", "main")
    _g(work, "config", "user.email", "t@t.io")
    _g(work, "config", "user.name", "t")
    (work / "SKILL.md").write_text(
        f"---\nname: gitskill\nversion: {version}\n---\n\n# git\n", encoding="utf-8"
    )
    _g(work, "add", "-A")
    _g(work, "commit", "-q", "-m", "init")
    bare = tmp_path / f"origin-{name}.git"
    _g(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def test_from_git_meta_version_from_clone_frontmatter(tmp_path: Path) -> None:
    bare = _make_bare_repo(tmp_path, "v1", "9.9.9")
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)

    res = inst.install(
        slug="gitskill", version="0.0.0-local", commit_sha="",
        repo_url=bare.as_uri(), git_ref="",
        manifest={"version": "0.0.0-local", "files": []},
    )

    assert res.version == "9.9.9"
    meta = read_meta(store / "gitskill")
    assert meta["version"] == "9.9.9"
    assert meta["source"] == "git-url"


def test_from_git_update_detects_new_frontmatter_version(tmp_path: Path) -> None:
    bare1 = _make_bare_repo(tmp_path, "v1", "9.9.9")
    bare2 = _make_bare_repo(tmp_path, "v2", "10.0.0")
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)
    inst.install(
        slug="gitskill", version="0.0.0-local", commit_sha="",
        repo_url=bare1.as_uri(), git_ref="",
        manifest={"version": "0.0.0-local", "files": []},
    )

    res2 = inst.install(
        slug="gitskill", version="0.0.0-local", commit_sha="",
        repo_url=bare2.as_uri(), git_ref="",
        manifest={"version": "0.0.0-local", "files": []},
    )

    assert res2.is_update is True
    assert res2.version == "10.0.0"
    assert read_meta(store / "gitskill")["version"] == "10.0.0"


def test_from_git_no_frontmatter_version_falls_back(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git не установлен")

    def _g(cwd: Path, *args: str) -> None:
        subprocess.run([git, *args], cwd=str(cwd), check=True,
                       capture_output=True, text=True)

    work = tmp_path / "work-nv"
    work.mkdir()
    _g(work, "init", "-q", "-b", "main")
    _g(work, "config", "user.email", "t@t.io")
    _g(work, "config", "user.name", "t")
    (work / "SKILL.md").write_text("---\nname: nv\n---\n\n# nv\n", encoding="utf-8")
    _g(work, "add", "-A")
    _g(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin-nv.git"
    _g(tmp_path, "clone", "-q", "--bare", str(work), str(bare))

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    inst = SkillInstaller(target, store_dir=tmp_path / "store")
    res = inst.install(
        slug="nv", version="0.0.0-local", commit_sha="",
        repo_url=bare.as_uri(), git_ref="",
        manifest={"version": "0.0.0-local", "files": []},
    )
    assert res.version == "0.0.0-local"
