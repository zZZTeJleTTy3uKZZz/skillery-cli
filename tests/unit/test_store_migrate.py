"""migrate: copy-установки → стор + ссылка; чужое/уже-ссылки не трогаем."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli.core import linker
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller, write_meta


def _copy_install(base: Path, name: str) -> Path:
    """Имитирует старую copy-установку: папка с _skill_meta.json в scope."""
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("body", encoding="utf-8")
    write_meta(d, {"slug": name, "version": "1.0.0", "manifest": {}})
    return d


def test_migrate_converts_copy_to_store_link(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)
    base = target.base_dir()  # global
    _copy_install(base, "bitrix24")

    report = inst.migrate_scope()
    assert "bitrix24" in report["migrated"]
    assert (store / "bitrix24" / "SKILL.md").read_text(encoding="utf-8") == "body"
    assert linker.is_link(target.slug_dir("bitrix24"))


def test_migrate_skips_foreign_and_linked(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)
    base = target.base_dir()
    base.mkdir(parents=True, exist_ok=True)
    # foreign: папка без meta
    foreign = base / "hand-made"
    foreign.mkdir()
    (foreign / "x.txt").write_text("manual", encoding="utf-8")
    # already-linked: внешняя ссылка
    ext = tmp_path / "ext" / "foreign-skill"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text("y", encoding="utf-8")
    linker.create_link(base / "external", ext)

    report = inst.migrate_scope()
    assert "hand-made" in report["skipped_foreign"]
    assert "external" in report["skipped_linked"]
    assert (foreign / "x.txt").exists()             # не тронули
    assert linker.is_link(base / "external")        # не тронули


def test_migrate_dry_run_changes_nothing(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)
    base = target.base_dir()
    _copy_install(base, "wb-api")

    report = inst.migrate_scope(dry_run=True)
    assert "wb-api" in report["migrated"]
    # Но фактически ничего не изменилось.
    assert not (store / "wb-api").exists()
    assert not linker.is_link(target.slug_dir("wb-api"))
    assert (target.slug_dir("wb-api") / "SKILL.md").exists()  # копия на месте


def test_cmd_migrate_text_render_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Регрессия: cmd_migrate в TEXT-режиме рендерит payload['reports'], не падая.

    Раньше _render итерировал весь payload ({dry_run, reports}) → r['migrated']
    на bool → TypeError. JSON-режим баг не ловил (text_renderer не вызывается).
    """
    import skillery_cli.__main__ as main_mod
    from skillery_cli import output as out_mod
    from skillery_cli.config import ClientConfig

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    _copy_install(target.base_dir(), "demo")

    cfg = ClientConfig(store_dir=str(store))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "text")  # именно text-режим вызывает _render

    # Не должно бросить (раньше — TypeError на payload['dry_run']).
    main_mod.cmd_migrate(scope="global", project=None, dry_run=False, agent=None)

    out = capsys.readouterr().out
    assert "migrate global" in out
    assert linker.is_link(target.slug_dir("demo"))
