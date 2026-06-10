"""Фикс 3 (MAJOR): lifecycle-команды живут без логина.

Раньше enable/disable/remove/sync/migrate/store(list/path/gc) регистрировались
ТОЛЬКО в gated-блоке `skill.install` (после login) — на живом окружении
разлогиненный пользователь не мог даже снять ссылку / посмотреть стор.

Гарантии:
- build_app() с чистым (незалогиненным) конфигом регистрирует
  enable/disable/remove/sync/migrate + sub-app store;
- remove/disable/store list реально работают без логина (локальный стор);
- sync без логина: что есть в сторе — линкует, чего нет — в missing
  (с подсказкой залогиниться), НЕ падает целиком.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import skills_hub_cli.__main__ as main_mod
from skills_hub_cli import output as out_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core import linker, project_manifest as pm
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _clean_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
    cfg = ClientConfig(store_dir=str(tmp_path / "store"))
    assert cfg.is_logged_in() is False
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    return cfg


def test_build_app_registers_lifecycle_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_cfg(tmp_path, monkeypatch)
    app = main_mod.build_app()
    names = {cmd.name for cmd in app.registered_commands}
    assert {"install", "remove", "enable", "disable", "sync", "migrate"} <= names
    group_names = {g.name for g in app.registered_groups}
    assert "store" in group_names
    store_group = next(g for g in app.registered_groups if g.name == "store")
    sub = {c.name for c in store_group.typer_instance.registered_commands}
    assert {"list", "path", "gc"} <= sub


def test_remove_disable_store_list_work_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cfg = _clean_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(out_mod, "_mode", "json")
    project = tmp_path / "proj"
    project.mkdir()

    inst = SkillInstaller(target, store_dir=cfg.effective_store_dir())
    inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                 repo_url=None, manifest=_MANIFEST)               # global
    inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                 repo_url=None, manifest=_MANIFEST, project=project)  # project
    pm.add(project, "demo")

    # store list — без логина.
    main_mod.cmd_store_list()
    rows = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert any(r["name"] == "demo" for r in rows)

    # disable (project) — без логина.
    main_mod.cmd_disable(slug="demo", project=project, agent=None)
    assert not target.slug_dir("demo", project=project).exists()

    # remove (global) — без логина.
    main_mod.cmd_remove(slug="demo", scope="global", project=None,
                        keep_local=False, purge=False, agent=None)
    assert not target.slug_dir("demo").exists()
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["removed"] is True


def test_sync_without_login_links_store_and_marks_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cfg = _clean_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "json")
    project = tmp_path / "proj"
    project.mkdir()

    # В сторе есть "have"; "ghost" — только в манифесте.
    inst = SkillInstaller(target, store_dir=cfg.effective_store_dir())
    inst.install(slug="have", version="1.0.0", commit_sha="a1",
                 repo_url=None, manifest=_MANIFEST)
    pm.add(project, "have")
    pm.add(project, "ghost")

    # НЕ должен упасть (раньше валился на _get_access_token → typer.Exit).
    main_mod.cmd_sync(project=project, prune=True, agent=None, channel="published")

    assert linker.is_link(target.slug_dir("have", project=project))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "have" in payload["linked"]
    assert "ghost" in payload["missing"]
    # Подсказка залогиниться для докачки.
    assert "hint" in payload
    assert "login" in payload["hint"]
