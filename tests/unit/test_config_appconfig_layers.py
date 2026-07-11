"""cli-kits W4: ClientConfig.load/save поверх ``clikit.config.AppConfig``.

Проверяем, что перевод конфиг-слоя на 4-слойный AppConfig (defaults → файл
``~/.skillery/config.toml`` → env → overrides) НЕ ломает публичный контракт
ClientConfig: те же поля, дефолты, сигнатуры, TOML-формат файла, env-override
(``SKILLERY_BASE_URL`` / ``SKILLERY_STORE_DIR``), профили и derive web_ui.

Токен-функции (save/load/clear_tokens, W2) — НЕ предмет этих тестов.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from skillery_cli import config as config_mod
from skillery_cli.config import ClientConfig


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбросить env/профиль, чтобы тесты не зависели от окружения хоста."""
    for var in (
        "SKILLERY_BASE_URL",
        "SKILLERY_STORE_DIR",
        "SKILLERY_CONFIG_DIR",
        "SKILLERY_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    config_mod.set_active_profile(None)


# --------------------------------------------------------------------------
# дефолты dataclass (контракт полей)
# --------------------------------------------------------------------------
def test_defaults_unchanged() -> None:
    cfg = ClientConfig()
    # Свежий CLI без конфига/env бьёт в ПРОД, не в localhost (иначе browser-flow
    # логин открывал localhost:3000). Dev — env SKILLERY_BASE_URL=localhost:8000.
    assert cfg.base_url == "https://api.skillery.ru"  # из _default_base_url
    assert cfg.user_email is None
    assert cfg.agent is None
    assert cfg.permissions == []
    assert cfg.company_id is None
    assert cfg.role_id is None
    assert cfg.access_expires_at is None
    assert cfg.auto_update is True
    assert cfg.auto_update_cooldown_min == 60
    assert cfg.last_auto_update_at is None
    assert cfg.output_format == "text"
    assert cfg.default_install_scope == "project"
    assert cfg.default_project_dir is None
    assert cfg.store_dir is None
    assert cfg.web_ui_url is None


def test_base_url_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой base_url берётся из SKILLERY_BASE_URL (исторический контракт)."""
    monkeypatch.setenv("SKILLERY_BASE_URL", "https://api.example.com")
    assert ClientConfig().base_url == "https://api.example.com"


# --------------------------------------------------------------------------
# save → TOML-формат (байт-в-байт совместим с прежним)
# --------------------------------------------------------------------------
def test_save_writes_expected_toml(tmp_path: Path) -> None:
    cfg = ClientConfig(
        base_url="https://api.hub",
        user_email="a@hub",
        agent="claude",
        permissions=["skill.read", "hub.admin"],
        company_id="7",
        role_id="3",
        access_expires_at="2030-01-01T00:00:00+00:00",
        auto_update=False,
        auto_update_cooldown_min=15,
        last_auto_update_at="2026-01-01T00:00:00+00:00",
        output_format="json",
        default_install_scope="global",
        default_project_dir="/tmp/proj",
        store_dir="/tmp/store",
        web_ui_url="https://hub",
    )
    p = tmp_path / "config.toml"
    cfg.save(p)
    data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    assert data == {
        "base_url": "https://api.hub",
        "user_email": "a@hub",
        "agent": "claude",
        "permissions": ["skill.read", "hub.admin"],
        "company_id": "7",
        "role_id": "3",
        "access_expires_at": "2030-01-01T00:00:00+00:00",
        "auto_update": False,
        "auto_update_cooldown_min": 15,
        "last_auto_update_at": "2026-01-01T00:00:00+00:00",
        "output_format": "json",
        "default_install_scope": "global",
        "default_project_dir": "/tmp/proj",
        "store_dir": "/tmp/store",
        "web_ui_url": "https://hub",
    }


def test_save_omits_empty_optionals(tmp_path: Path) -> None:
    """None/пустые опц.поля НЕ пишутся (как прежний save)."""
    cfg = ClientConfig(base_url="http://localhost:8000")
    p = tmp_path / "config.toml"
    cfg.save(p)
    data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    # обязательные ключи
    assert data["base_url"] == "http://localhost:8000"
    assert data["auto_update"] is True
    assert data["auto_update_cooldown_min"] == 60
    assert data["output_format"] == "text"
    assert data["default_install_scope"] == "project"
    # опциональные — отсутствуют
    for absent in (
        "user_email",
        "agent",
        "permissions",
        "company_id",
        "role_id",
        "access_expires_at",
        "last_auto_update_at",
        "default_project_dir",
        "store_dir",
        "web_ui_url",
    ):
        assert absent not in data, absent


# --------------------------------------------------------------------------
# round-trip save/load
# --------------------------------------------------------------------------
def test_round_trip(tmp_path: Path) -> None:
    cfg = ClientConfig(
        base_url="https://api.hub",
        user_email="a@hub",
        permissions=["skill.read"],
        auto_update=False,
        auto_update_cooldown_min=5,
        output_format="json",
        default_install_scope="global",
        store_dir="/tmp/s",
    )
    p = tmp_path / "config.toml"
    cfg.save(p)
    loaded = ClientConfig.load(p)
    assert loaded == cfg


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    loaded = ClientConfig.load(tmp_path / "nope.toml")
    assert loaded == ClientConfig()


def test_load_explicit_path_ignores_env_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """base_url из файла приоритетнее env-дефолта для load(path)."""
    monkeypatch.setenv("SKILLERY_BASE_URL", "https://env.example")
    p = tmp_path / "config.toml"
    p.write_text('base_url = "https://file.example"\n', encoding="utf-8")
    assert ClientConfig.load(p).base_url == "https://file.example"


def test_load_bom_file(tmp_path: Path) -> None:
    """Файл с UTF-8 BOM читается (прежний load делал utf-8-sig)."""
    p = tmp_path / "config.toml"
    p.write_bytes(b"\xef\xbb\xbf" + b'base_url = "https://bom.hub"\n')
    assert ClientConfig.load(p).base_url == "https://bom.hub"


def test_load_ignores_unknown_keys(tmp_path: Path) -> None:
    """Forward-compat: незнакомые ключи (или legacy-флаги) не ломают load."""
    p = tmp_path / "config.toml"
    p.write_text(
        'base_url = "http://localhost:8000"\n'
        'permissions = ["skill.read"]\n'
        "is_hub_admin = true\n"
        'future_field = "x"\n',
        encoding="utf-8",
    )
    loaded = ClientConfig.load(p)
    assert loaded.permissions == ["skill.read"]
    assert not hasattr(loaded, "future_field")


# --------------------------------------------------------------------------
# default path + env-override каталога
# --------------------------------------------------------------------------
def test_default_config_path_respects_config_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(tmp_path))
    config_mod.set_active_profile(None)
    cfg = ClientConfig(base_url="http://localhost:8000", user_email="x@hub")
    cfg.save()  # без path → дефолтный файл
    written = tmp_path / "config.toml"
    assert written.is_file()
    assert ClientConfig.load().user_email == "x@hub"


def test_default_path_uses_profile_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(tmp_path))
    config_mod.set_active_profile("work")
    try:
        cfg = ClientConfig(base_url="http://localhost:8000", user_email="w@hub")
        cfg.save()
        assert (tmp_path / "profiles" / "work" / "config.toml").is_file()
        assert ClientConfig.load().user_email == "w@hub"
    finally:
        config_mod.set_active_profile(None)


# --------------------------------------------------------------------------
# ребренд home-каталога ~/.skillery → ~/.skillery + обратная совместимость
# --------------------------------------------------------------------------
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный home (без env-override каталога) для тестов резолвера."""
    monkeypatch.delenv("SKILLERY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SKILLERY_STORE_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows expanduser
    return home


def test_fresh_install_defaults_to_skillery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Свежая установка (нет ни ~/.skillery, ни ~/.skillery) → ~/.skillery."""
    home = _isolate_home(tmp_path, monkeypatch)
    assert config_mod._default_config_dir() == home / ".skillery"
    assert config_mod._default_store_dir() == home / ".skillery" / "store"


def test_legacy_skills_hub_used_when_new_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обратная совместимость: есть старый ~/.skillery, нового нет → legacy."""
    home = _isolate_home(tmp_path, monkeypatch)
    (home / ".skillery").mkdir()
    assert config_mod._default_config_dir() == home / ".skillery"


def test_env_override_beats_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env ``SKILLERY_CONFIG_DIR`` приоритетнее legacy-каталога (override)."""
    home = _isolate_home(tmp_path, monkeypatch)
    (home / ".skillery").mkdir()
    override = tmp_path / "explicit-cfg"
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(override))
    assert config_mod._default_config_dir() == override


def test_save_fresh_writes_skillery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save() пишет config в ~/.skillery."""
    home = _isolate_home(tmp_path, monkeypatch)
    config_mod.set_active_profile(None)
    ClientConfig(base_url="http://localhost:8000", user_email="x@hub").save()
    assert (home / ".skillery" / "config.toml").is_file()
    assert ClientConfig.load().user_email == "x@hub"


# --------------------------------------------------------------------------
# методы-предикаты и derive (без изменений)
# --------------------------------------------------------------------------
def test_effective_store_dir_explicit() -> None:
    cfg = ClientConfig(store_dir="/data/store")
    assert cfg.effective_store_dir() == Path("/data/store")


def test_effective_store_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLERY_STORE_DIR", "/env/store")
    cfg = ClientConfig()
    assert cfg.effective_store_dir() == Path("/env/store")


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://api.hub.example", "https://hub.example"),
        ("http://api.local", "http://local"),
        ("http://localhost:8000", "http://localhost:3000"),
        ("https://hub.example", "https://hub.example"),
    ],
)
def test_effective_web_ui_url_derive(base: str, expected: str) -> None:
    assert ClientConfig(base_url=base).effective_web_ui_url() == expected


def test_effective_web_ui_url_explicit_override() -> None:
    cfg = ClientConfig(base_url="https://api.hub", web_ui_url="https://custom.ui")
    assert cfg.effective_web_ui_url() == "https://custom.ui"


def test_is_logged_in() -> None:
    assert ClientConfig().is_logged_in() is False
    assert ClientConfig(user_email="a@hub").is_logged_in() is False
    assert (
        ClientConfig(user_email="a@hub", permissions=["skill.read"]).is_logged_in()
        is True
    )


def test_has_permission_and_admin_wildcard() -> None:
    cfg = ClientConfig(permissions=["hub.admin"])
    assert cfg.has_permission("anything.at.all") is True
    cfg2 = ClientConfig(permissions=["skill.read"])
    assert cfg2.has_permission("skill.read") is True
    assert cfg2.has_permission("skill.write") is False
