"""Тесты проектного манифеста .skillery/skills.toml (+ legacy .skillery)."""
from __future__ import annotations

from pathlib import Path

from skillery_cli.core import project_manifest as pm


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    assert pm.load(tmp_path) == {}
    assert pm.list_(tmp_path) == []


def test_add_creates_manifest_and_lists(tmp_path: Path) -> None:
    pm.add(tmp_path, "bitrix24")
    assert (tmp_path / ".skillery" / "skills.toml").exists()
    assert pm.load(tmp_path) == {"bitrix24": "*"}
    assert pm.list_(tmp_path) == ["bitrix24"]


def test_load_reads_legacy_skills_hub_when_no_new(tmp_path: Path) -> None:
    """Обратная совместимость: старый .skillery/skills.toml читается, если
    нового .skillery нет (проект с прежней установкой не теряет набор)."""
    legacy = tmp_path / ".skillery"
    legacy.mkdir()
    (legacy / "skills.toml").write_text(
        '[skills]\nbitrix24 = "*"\nwb-api = "*"\n', encoding="utf-8"
    )
    assert pm.load(tmp_path) == {"bitrix24": "*", "wb-api": "*"}
    assert pm.list_(tmp_path) == ["bitrix24", "wb-api"]


def test_add_migrates_legacy_entries_into_new_manifest(tmp_path: Path) -> None:
    """Первая запись в проекте с legacy .skillery переносит его записи в
    новый .skillery (load fallback'ит на legacy → save пишет всё в новый).

    Так набор навыков старого проекта не теряется при первом add/remove."""
    (tmp_path / ".skillery").mkdir()
    (tmp_path / ".skillery" / "skills.toml").write_text(
        '[skills]\nold = "*"\n', encoding="utf-8"
    )
    pm.add(tmp_path, "new")
    # Новый каталог получает И legacy-навык, И добавленный.
    assert (tmp_path / ".skillery" / "skills.toml").exists()
    assert pm.load(tmp_path) == {"new": "*", "old": "*"}


def test_add_is_idempotent_and_sorted(tmp_path: Path) -> None:
    pm.add(tmp_path, "wb-api")
    pm.add(tmp_path, "bitrix24")
    pm.add(tmp_path, "wb-api")  # повтор
    assert pm.list_(tmp_path) == ["bitrix24", "wb-api"]


def test_remove(tmp_path: Path) -> None:
    pm.add(tmp_path, "bitrix24")
    pm.add(tmp_path, "wb-api")
    assert pm.remove(tmp_path, "bitrix24") is True
    assert pm.list_(tmp_path) == ["wb-api"]
    assert pm.remove(tmp_path, "ghost") is False


def test_load_tolerates_bom(tmp_path: Path) -> None:
    d = tmp_path / ".skillery"
    d.mkdir()
    (d / "skills.toml").write_text(
        '﻿[skills]\nbitrix24 = "*"\n', encoding="utf-8"
    )
    assert pm.load(tmp_path) == {"bitrix24": "*"}
