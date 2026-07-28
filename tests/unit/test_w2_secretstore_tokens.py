"""cli-kits W2: токены CLI поверх ``librarykit.SecretStore``.

Контракт обратной совместимости (КРИТИЧНО — без разлогина прода):
``save_tokens``/``load_tokens``/``clear_tokens`` переписаны поверх
``librarykit.secret_store.SecretStore``, но СОХРАНЯЮТ прежний контракт хранения:

- keyring-namespace = ``skillery-cli`` (или ``skillery-cli:<profile>``) —
  тот же, что был → ранее записанные keyring-ключи читаются как есть;
- ключи keyring = ``"{email}:access"`` / ``"{email}:refresh"`` — без изменений;
- file-fallback = ``~/.skillery/[profiles/<p>/]tokens.toml`` тот же путь +
  формат ``{user_email, access, refresh}`` → уже сохранённый файл читается;
- env-override ``SKILLERY_ACCESS_TOKEN`` / ``SKILLERY_REFRESH_TOKEN``
  читается ПЕРВЫМ (как раньше).

Эти тесты доказывают: СТАРЫЙ tokens.toml + СТАРЫЕ keyring-ключи читаются новым
кодом → залогиненный прод-юзер остаётся залогинен.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomli_w

from skillery_cli import config as config_module

EMAIL = "user@example.com"
LONG_ACCESS = "eyJ." + "a" * 1600
LONG_REFRESH = "eyJ." + "r" * 1500


class _FakeKeyring:
    """In-memory keyring (тот же контракт, что в test_p0fix_token_fallback)."""

    def __init__(self, *, fail_get: bool = False) -> None:
        self.storage: dict[tuple[str, str], str] = {}
        self._fail_get = fail_get

    def set_password(self, service: str, username: str, value: str) -> None:
        self.storage[(service, username)] = value

    def get_password(self, service: str, username: str) -> str | None:
        if self._fail_get:
            raise OSError(1783, "CredRead failure")
        return self.storage.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.storage:
            raise RuntimeError("PasswordDeleteError: item not found")
        del self.storage[(service, username)]


@pytest.fixture()
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SKILLERY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SKILLERY_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("SKILLERY_PROFILE", raising=False)
    monkeypatch.setattr(config_module, "_ACTIVE_PROFILE", None)
    return tmp_path


def _tokens_file() -> Path:
    return config_module._default_config_dir() / "tokens.toml"


# ============================================================
# 1. Реализация делегирует в librarykit.SecretStore
# ============================================================
def test_save_tokens_built_on_librarykit_secretstore() -> None:
    """save/load токенов реализованы поверх ``librarykit.SecretStore``.

    Доказываем, что secret-слой — это SecretStore, а не своя копия логики:
    модуль импортирует класс из кита-владельца.
    """
    from librarykit.secret_store import SecretStore

    assert getattr(config_module, "SecretStore", None) is SecretStore


# ============================================================
# 2. Обратная совместимость: СТАРЫЙ tokens.toml читается новым load_tokens
# ============================================================
def test_legacy_tokens_toml_is_read_by_new_load_tokens(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Существующий ``~/.skillery/tokens.toml`` (старый формат и путь).

    Записан ДО апдейта CLI — новый load_tokens обязан вернуть ту же пару
    (иначе прод-юзер разлогинится). keyring пуст → читается файл.
    """
    # Файл в точности как пишет СТАРЫЙ config.py: {user_email, access, refresh}.
    path = _tokens_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        tomli_w.dumps(
            {"user_email": EMAIL, "access": LONG_ACCESS, "refresh": LONG_REFRESH}
        ),
        encoding="utf-8",
    )
    fake = _FakeKeyring()  # keyring пуст → должен дочитать файл
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    access, refresh = config_module.load_tokens(EMAIL)
    assert access == LONG_ACCESS
    assert refresh == LONG_REFRESH


def test_legacy_keyring_keys_are_read_by_new_load_tokens(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """СТАРЫЕ keyring-ключи (namespace ``skillery-cli``, ключи
    ``{email}:access``/``{email}:refresh``) читаются новым кодом as-is.
    """
    fake = _FakeKeyring()
    # Имитируем то, что записал СТАРЫЙ config.py: namespace == KEYRING_SERVICE.
    fake.storage[(config_module.KEYRING_SERVICE, f"{EMAIL}:access")] = "KR_A"
    fake.storage[(config_module.KEYRING_SERVICE, f"{EMAIL}:refresh")] = "KR_R"
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    access, refresh = config_module.load_tokens(EMAIL)
    assert access == "KR_A"
    assert refresh == "KR_R"


# ============================================================
# 3. Round-trip save → load (file-fallback путь — рабочий прод-путь)
# ============================================================
def test_roundtrip_via_keyring(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    config_module.save_tokens(EMAIL, "A1", "R1")
    assert config_module.load_tokens(EMAIL) == ("A1", "R1")


def test_roundtrip_via_file_fallback_when_no_keyring(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keyring недоступен (None) → file-fallback на запись И на чтение.

    Это рабочий прод-путь на машине пользователя (keyring не пишет).
    """
    monkeypatch.setattr(config_module, "_try_keyring", lambda: None)

    config_module.save_tokens(EMAIL, LONG_ACCESS, LONG_REFRESH)
    # Файл лежит по каноничному пути и в каноничном формате.
    data = tomllib.loads(_tokens_file().read_text(encoding="utf-8-sig"))
    assert data == {
        "user_email": EMAIL,
        "access": LONG_ACCESS,
        "refresh": LONG_REFRESH,
    }
    assert config_module.load_tokens(EMAIL) == (LONG_ACCESS, LONG_REFRESH)


# ============================================================
# 4. env-override читается ПЕРВЫМ (как раньше)
# ============================================================
def test_env_override_wins_over_stored(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILLERY_ACCESS_TOKEN", "ENV_A")
    monkeypatch.setenv("SKILLERY_REFRESH_TOKEN", "ENV_R")
    fake = _FakeKeyring()
    fake.storage[(config_module.KEYRING_SERVICE, f"{EMAIL}:access")] = "KR_A"
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    assert config_module.load_tokens(EMAIL) == ("ENV_A", "ENV_R")


# ============================================================
# 5. profile-namespace: профиль изолирует токены (тот же формат, что был)
# ============================================================
def test_profile_uses_namespaced_keyring(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Активный профиль → namespace ``skillery-cli:<profile>`` (как раньше)."""
    config_module.set_active_profile("work")
    try:
        fake = _FakeKeyring()
        monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

        config_module.save_tokens(EMAIL, "PA", "PR")

        ns = f"{config_module.KEYRING_SERVICE}:work"
        assert fake.storage[(ns, f"{EMAIL}:access")] == "PA"
        assert fake.storage[(ns, f"{EMAIL}:refresh")] == "PR"
        assert config_module.load_tokens(EMAIL) == ("PA", "PR")
    finally:
        config_module.set_active_profile(None)


def test_profile_file_fallback_goes_under_profiles_dir(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """profile + keyring=None → tokens.toml под ``profiles/<p>/`` (как раньше)."""
    config_module.set_active_profile("work")
    try:
        monkeypatch.setattr(config_module, "_try_keyring", lambda: None)

        config_module.save_tokens(EMAIL, "PA", "PR")

        expected = isolated_config_dir / "profiles" / "work" / "tokens.toml"
        assert expected.exists()
        assert config_module.load_tokens(EMAIL) == ("PA", "PR")
    finally:
        config_module.set_active_profile(None)
