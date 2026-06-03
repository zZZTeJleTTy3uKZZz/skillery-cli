"""Тесты проектного манифеста .skills-hub/skills.toml."""
from __future__ import annotations

from pathlib import Path

from skills_hub_cli.core import project_manifest as pm


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    assert pm.load(tmp_path) == {}
    assert pm.list_(tmp_path) == []


def test_add_creates_manifest_and_lists(tmp_path: Path) -> None:
    pm.add(tmp_path, "bitrix24")
    assert (tmp_path / ".skills-hub" / "skills.toml").exists()
    assert pm.load(tmp_path) == {"bitrix24": "*"}
    assert pm.list_(tmp_path) == ["bitrix24"]


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
    d = tmp_path / ".skills-hub"
    d.mkdir()
    (d / "skills.toml").write_text(
        '﻿[skills]\nbitrix24 = "*"\n', encoding="utf-8"
    )
    assert pm.load(tmp_path) == {"bitrix24": "*"}
