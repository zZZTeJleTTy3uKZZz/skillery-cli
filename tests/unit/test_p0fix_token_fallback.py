"""P0-фикс [BLOCKER]: Windows keyring CredWrite WinError 1783 на длинных JWT.

Факт с живого Windows 11: Credential Manager ограничивает blob 2560 байт
(UTF-16 → ~1280 символов). hub_admin access-JWT = 1519 символов →
``keyring.set_password`` бросает ``OSError(1783)`` → raw traceback,
авторизация невозможна (login и set-tokens).

Требуемое поведение (config.py):
- ``save_tokens``: ЛЮБАЯ ошибка записи в keyring → оба токена в
  file-fallback ``tokens.toml`` + один аккуратный warning в stderr
  (не traceback). При partial-write (access записался, refresh упал) —
  не оставлять рассинхрон: best-effort удалить keyring-ключи.
- ``load_tokens``: keyring вернул None / бросил → читать file-fallback
  (раньше файл читался ТОЛЬКО при ImportError keyring).
- ``clear_tokens``: чистить ОБА хранилища best-effort.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomli_w

from skillery_cli import config as config_module

EMAIL = "admin@hub.ru"
# Реалистичная длина: hub_admin access-JWT с живого стенда = 1519 символов.
LONG_ACCESS = "eyJ." + "a" * 1600
LONG_REFRESH = "eyJ." + "r" * 1500


class _FakeKeyring:
    """In-memory keyring; set/get могут бросать OSError как CredWrite/CredRead."""

    def __init__(
        self,
        *,
        fail_set_substrings: tuple[str, ...] = (),
        fail_get: bool = False,
    ) -> None:
        self.storage: dict[tuple[str, str], str] = {}
        self.set_calls: list[str] = []
        self.delete_calls: list[str] = []
        self._fail_set_substrings = fail_set_substrings
        self._fail_get = fail_get

    def set_password(self, service: str, username: str, value: str) -> None:
        self.set_calls.append(username)
        if any(s in username for s in self._fail_set_substrings):
            raise OSError(1783, "Стаб получил неверно сформированные данные (CredWrite)")
        self.storage[(service, username)] = value

    def get_password(self, service: str, username: str) -> str | None:
        if self._fail_get:
            raise OSError(1783, "CredRead failure")
        return self.storage.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.delete_calls.append(username)
        if (service, username) not in self.storage:
            raise RuntimeError("PasswordDeleteError: item not found")
        del self.storage[(service, username)]


@pytest.fixture()
def isolated_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SKILLERY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SKILLERY_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("SKILLERY_PROFILE", raising=False)
    monkeypatch.setattr(config_module, "_ACTIVE_PROFILE", None)
    return tmp_path


def _tokens_file() -> Path:
    return config_module._default_config_dir() / "tokens.toml"


def _read_tokens_toml() -> dict:
    return tomllib.loads(_tokens_file().read_text(encoding="utf-8-sig"))


def _write_tokens_toml(access: str, refresh: str) -> None:
    path = _tokens_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        tomli_w.dumps(
            {"user_email": EMAIL, "access": access, "refresh": refresh}
        ),
        encoding="utf-8",
    )


# ============================================================
# save_tokens: ошибка записи keyring → file-fallback обоих токенов
# ============================================================
def test_save_tokens_keyring_write_error_falls_back_to_file(
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OSError(1783) на первом же set_password → оба токена в tokens.toml."""
    fake = _FakeKeyring(fail_set_substrings=(":access", ":refresh"))
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    # Не должно бросить (раньше: raw traceback OSError 1783)
    config_module.save_tokens(EMAIL, LONG_ACCESS, LONG_REFRESH)

    data = _read_tokens_toml()
    assert data["access"] == LONG_ACCESS
    assert data["refresh"] == LONG_REFRESH
    assert data["user_email"] == EMAIL


def test_save_tokens_keyring_write_error_warns_once_in_stderr(
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Один tidy warning в stderr, без traceback."""
    fake = _FakeKeyring(fail_set_substrings=(":access",))
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    config_module.save_tokens(EMAIL, LONG_ACCESS, LONG_REFRESH)

    captured = capsys.readouterr()
    err_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(err_lines) == 1, f"ожидали 1 warning-строку, получили: {err_lines}"
    assert "tokens.toml" in err_lines[0]
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_save_tokens_partial_write_cleans_keyring_and_writes_both(
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """access записался, refresh упал → НЕ оставляем рассинхрон.

    Оба токена пишутся в файл, а возможно записанные keyring-ключи
    best-effort удаляются.
    """
    fake = _FakeKeyring(fail_set_substrings=(":refresh",))
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    config_module.save_tokens(EMAIL, LONG_ACCESS, LONG_REFRESH)

    # Оба токена в файле
    data = _read_tokens_toml()
    assert data["access"] == LONG_ACCESS
    assert data["refresh"] == LONG_REFRESH
    # Best-effort cleanup: попытка удалить оба ключа из keyring
    assert f"{EMAIL}:access" in fake.delete_calls
    assert f"{EMAIL}:refresh" in fake.delete_calls
    # Частично записанный access реально удалён из keyring
    assert not any(":access" in user for (_, user) in fake.storage)


def test_save_tokens_keyring_success_does_not_warn_and_removes_stale_file(
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Успешная запись в keyring: stale file-fallback подчищается,

    чтобы load_tokens при сбое чтения keyring не вернул СТАРУЮ пару.
    """
    _write_tokens_toml("OLD_ACCESS", "OLD_REFRESH")
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    config_module.save_tokens(EMAIL, LONG_ACCESS, LONG_REFRESH)

    ns = config_module._keyring_namespace()
    assert fake.storage[(ns, f"{EMAIL}:access")] == LONG_ACCESS
    assert fake.storage[(ns, f"{EMAIL}:refresh")] == LONG_REFRESH
    assert not _tokens_file().exists()
    assert capsys.readouterr().err.strip() == ""


# ============================================================
# load_tokens: keyring None/бросил → file-fallback
# ============================================================
def test_load_tokens_falls_back_to_file_when_keyring_returns_none(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keyring установлен, но пуст (запись ушла в файл) → читаем файл."""
    _write_tokens_toml(LONG_ACCESS, LONG_REFRESH)
    fake = _FakeKeyring()  # get_password вернёт None
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    access, refresh = config_module.load_tokens(EMAIL)
    assert access == LONG_ACCESS
    assert refresh == LONG_REFRESH


def test_load_tokens_falls_back_to_file_when_keyring_raises(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeKeyring(fail_get=True)
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)
    _write_tokens_toml(LONG_ACCESS, LONG_REFRESH)

    access, refresh = config_module.load_tokens(EMAIL)
    assert access == LONG_ACCESS
    assert refresh == LONG_REFRESH


def test_load_tokens_prefers_keyring_when_present(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Канонический путь не сломан: токены из keyring приоритетнее файла."""
    fake = _FakeKeyring()
    ns = config_module._keyring_namespace()
    fake.storage[(ns, f"{EMAIL}:access")] = "KR_ACCESS"
    fake.storage[(ns, f"{EMAIL}:refresh")] = "KR_REFRESH"
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)
    _write_tokens_toml("FILE_ACCESS", "FILE_REFRESH")

    access, refresh = config_module.load_tokens(EMAIL)
    assert access == "KR_ACCESS"
    assert refresh == "KR_REFRESH"


def test_load_tokens_returns_none_when_both_stores_empty(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    assert config_module.load_tokens(EMAIL) == (None, None)


# ============================================================
# Roundtrip: живой сценарий бага — save при сломанном keyring → load
# ============================================================
def test_save_load_roundtrip_via_file_with_broken_keyring(
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Сценарий живого e2e: CredWrite падает → login обязан выживать.

    save → file-fallback; load (keyring пуст) → та же пара из файла.
    """
    fake = _FakeKeyring(fail_set_substrings=(":access", ":refresh"))
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)

    config_module.save_tokens(EMAIL, LONG_ACCESS, LONG_REFRESH)
    access, refresh = config_module.load_tokens(EMAIL)

    assert access == LONG_ACCESS
    assert refresh == LONG_REFRESH


# ============================================================
# clear_tokens: чистит ОБА хранилища best-effort
# ============================================================
def test_clear_tokens_clears_both_keyring_and_file(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeKeyring()
    ns = config_module._keyring_namespace()
    fake.storage[(ns, f"{EMAIL}:access")] = "A"
    fake.storage[(ns, f"{EMAIL}:refresh")] = "R"
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)
    _write_tokens_toml(LONG_ACCESS, LONG_REFRESH)

    config_module.clear_tokens(EMAIL)

    assert f"{EMAIL}:access" in fake.delete_calls
    assert f"{EMAIL}:refresh" in fake.delete_calls
    assert not any(user.startswith(EMAIL) for (_, user) in fake.storage)
    assert not _tokens_file().exists()


def test_clear_tokens_survives_keyring_delete_errors(
    isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_password бросает (ключей нет) → файл всё равно удаляется."""
    fake = _FakeKeyring()  # storage пуст → delete_password бросит
    monkeypatch.setattr(config_module, "_try_keyring", lambda: fake)
    _write_tokens_toml(LONG_ACCESS, LONG_REFRESH)

    config_module.clear_tokens(EMAIL)  # не должно бросить

    assert not _tokens_file().exists()
