"""migrate: copy-установки → стор + ссылка; чужое/уже-ссылки не трогаем."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills_hub_cli.core import linker
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller, write_meta


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
