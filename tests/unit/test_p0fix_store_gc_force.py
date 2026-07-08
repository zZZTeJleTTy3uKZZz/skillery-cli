"""Фикс 4 (MAJOR): store gc — default dry-run + --force (доделка P0-фикса B9).

Живой факт: дефолтный gc удалил скиллы, на которые ссылались только
project-junction'ы (project-scope не сканируется) → битые ссылки в проектах.

Гарантии:
- БЕЗ --force gc только показывает кандидатов (ничего не удаляет);
- --dry-run остаётся алиасом дефолта (deprecated);
- удаление происходит ТОЛЬКО под --force;
- --dry-run сильнее --force (явный dry-run не удаляет);
- прямой вызов без аргументов (OptionInfo-дефолты) НЕ удаляет.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _gc_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Стор: referenced (есть global-ссылка) + orphan (ссылок нет)."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)
    inst.install(slug="referenced", version="1.0.0", commit_sha="a1",
                 repo_url=None, manifest=_MANIFEST)
    orphan = store / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("hi", encoding="utf-8")
    (orphan / "_skill_meta.json").write_text("{}", encoding="utf-8")

    cfg = ClientConfig(store_dir=str(store))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "json")
    return store


def _last_payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_gc_default_lists_candidates_but_does_not_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    store = _gc_env(tmp_path, monkeypatch)
    main_mod.cmd_store_gc(dry_run=False, force=False)
    payload = _last_payload(capsys)
    assert "orphan" in payload["candidates"]
    assert "referenced" not in payload["candidates"]
    assert payload["deleted"] is False
    assert payload["dry_run"] is True  # дефолт = только показ
    assert (store / "orphan").exists()  # НИЧЕГО не удалено


def test_gc_force_deletes_orphans_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    store = _gc_env(tmp_path, monkeypatch)
    main_mod.cmd_store_gc(dry_run=False, force=True)
    payload = _last_payload(capsys)
    assert "orphan" in payload["candidates"]
    assert payload["deleted"] is True
    assert not (store / "orphan").exists()
    assert (store / "referenced").exists()


def test_gc_dry_run_alias_still_works_and_overrides_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    store = _gc_env(tmp_path, monkeypatch)
    main_mod.cmd_store_gc(dry_run=True, force=True)
    payload = _last_payload(capsys)
    assert "orphan" in payload["candidates"]
    assert payload["deleted"] is False
    assert (store / "orphan").exists()


def test_gc_direct_call_with_defaults_does_not_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Прямой вызов cmd_store_gc() (как делают тесты/скрипты) получает
    OptionInfo-дефолты typer — они НЕ должны трактоваться как force=True."""
    store = _gc_env(tmp_path, monkeypatch)
    main_mod.cmd_store_gc()
    payload = _last_payload(capsys)
    assert "orphan" in payload["candidates"]
    assert payload["deleted"] is False
    assert (store / "orphan").exists()
