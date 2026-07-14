"""device_uid() — стабильный client_device_id устройства (web↔CLI мост)."""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core import identity


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SKILLERY_PROFILE", raising=False)
    identity.reset_cache()
    yield
    identity.reset_cache()


def test_device_uid_persists_and_is_stable(tmp_path: Path) -> None:
    first = identity.device_uid()
    assert first
    # Записан в отдельный файл device_id (НЕ в config.toml).
    f = tmp_path / "device_id"
    assert f.is_file()
    assert f.read_text(encoding="utf-8").strip() == first
    # Повторный вызов (сбросив кэш — как новый процесс) читает тот же id.
    identity.reset_cache()
    assert identity.device_uid() == first


def test_device_uid_respects_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(a))
    identity.reset_cache()
    id_a = identity.device_uid()
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(b))
    identity.reset_cache()
    id_b = identity.device_uid()
    assert id_a != id_b  # разные каталоги → разные машины


def test_device_uid_ignores_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """id — на машину: профиль не должен влиять (иначе UA≠register)."""
    from skillery_cli import config as config_mod

    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(tmp_path))
    config_mod.set_active_profile(None)
    identity.reset_cache()
    base_id = identity.device_uid()
    config_mod.set_active_profile("work")
    identity.reset_cache()
    assert identity.device_uid() == base_id
    config_mod.set_active_profile(None)


def test_fallback_is_deterministic_from_hostname() -> None:
    assert identity._fallback_uid() == identity._fallback_uid()
    assert identity._fallback_uid().startswith("h")
