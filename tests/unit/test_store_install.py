"""Тесты модели стор+линк: материализация в стор и линковка в scope."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from skills_hub_cli.config import ClientConfig


def test_effective_store_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SKILLS_HUB_STORE_DIR", str(tmp_path / "custom-store"))
    cfg = ClientConfig()
    assert cfg.effective_store_dir() == tmp_path / "custom-store"


def test_effective_store_dir_explicit_field(tmp_path: Path) -> None:
    cfg = ClientConfig(store_dir=str(tmp_path / "explicit"))
    assert cfg.effective_store_dir() == tmp_path / "explicit"


def test_effective_store_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILLS_HUB_STORE_DIR", raising=False)
    cfg = ClientConfig()
    # Дефолт — ~/.skills-hub/store (раскрытый).
    assert cfg.effective_store_dir().name == "store"
    assert ".skills-hub" in str(cfg.effective_store_dir())
