"""Логин: валидация e-mail ДО сохранения + внятная протухшая сессия.

Живой профиль владельца: ``user_email = "1"``. Причина — JWT-slim: ``sub`` в
токене это ЧИСЛОВОЙ user_id, а code/browser-flow писали его как почту. Токены
ложились в keyring под ключом «1», а почта из ``/me`` (её проставляет
``hydrate_session_permissions``) затиралась мусором.

Второе: когда access есть, а refresh нет (или сервер его отверг), наружу летело
голое ``401 Signature has expired`` — человек не понимал, что достаточно
повторить ``skillery login``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import typer

from skillery_cli import __main__ as m
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import ApiError


@pytest.fixture(autouse=True)
def _text_mode(monkeypatch) -> None:
    from skillery_cli import output as out

    monkeypatch.setattr(out, "is_json", lambda: False)
    monkeypatch.setattr(m, "is_json", lambda: False)
    m._REFRESH_FAILURE["reason"] = None


# --------------------------------------------------------------------------- #
# _is_valid_email / _resolve_login_email
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,ok",
    [
        ("ivan@acme.ru", True),
        ("u@e.co", True),
        ("1", False),
        ("", False),
        (None, False),
        (42, False),
        ("no-at-sign.ru", False),
        ("no@domain", False),
        ("two@@at.ru", False),
        ("with space@acme.ru", False),
    ],
)
def test_is_valid_email(value: Any, ok: bool) -> None:
    assert m._is_valid_email(value) is ok


def test_resolve_prefers_me_email_over_numeric_sub() -> None:
    """/me уже дал почту → числовой ``sub`` её НЕ затирает."""
    cfg = ClientConfig(base_url="x")
    cfg.user_email = "ivan@acme.ru"
    assert m._resolve_login_email(cfg, {"sub": "1"}) == "ivan@acme.ru"


def test_resolve_falls_back_to_jwt_email_claim() -> None:
    cfg = ClientConfig(base_url="x")
    assert m._resolve_login_email(cfg, {"sub": "1", "email": "a@b.io"}) == "a@b.io"


def test_resolve_accepts_sub_when_it_is_really_an_email() -> None:
    cfg = ClientConfig(base_url="x")
    assert m._resolve_login_email(cfg, {"sub": "a@b.io"}) == "a@b.io"


def test_resolve_returns_empty_for_garbage() -> None:
    cfg = ClientConfig(base_url="x")
    assert m._resolve_login_email(cfg, {"sub": "1"}) == ""


# --------------------------------------------------------------------------- #
# code-flow: мусорный sub не попадает ни в keyring, ни в конфиг
# --------------------------------------------------------------------------- #
def _wire_code_login(monkeypatch, cfg: ClientConfig, saved: dict, sub: str) -> None:
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: saved.update({"saved": True}))
    monkeypatch.setattr(
        m, "save_tokens",
        lambda email, access, refresh: saved.update({"email": email}),
    )
    monkeypatch.setattr(m, "populate_from_jwt", lambda c, t: None)
    monkeypatch.setattr(m, "decode_jwt_claims", lambda t: {"sub": sub})
    monkeypatch.setattr(m, "_ensure_daemon_after_login", lambda: {"event": "skipped"})

    client = MagicMock()

    async def _redeem(*, code: str) -> dict[str, Any]:
        return {"access_token": "acc", "refresh_token": "ref"}

    async def _close() -> None:
        return None

    async def _noop(*a: Any, **k: Any) -> None:
        return None

    client.exchange_redeem = _redeem
    client.close = _close
    monkeypatch.setattr(m, "HubClient", lambda **kw: client)
    monkeypatch.setattr(m, "hydrate_session_permissions", _noop)
    monkeypatch.setattr(m, "register_device_best_effort", _noop)


def test_code_login_rejects_numeric_sub_and_writes_nothing(monkeypatch) -> None:
    cfg = ClientConfig(base_url="http://localhost:8000")
    saved: dict = {}
    _wire_code_login(monkeypatch, cfg, saved, sub="1")

    with pytest.raises(typer.Exit):
        m._do_code_login(cfg, code="ABC")

    assert saved == {}, "мусорная почта уехала в keyring/конфиг"
    assert cfg.user_email is None


def test_code_login_keeps_email_resolved_from_me(monkeypatch) -> None:
    """Почта из /me пережила логин, хотя в JWT ``sub`` — числовой id."""
    cfg = ClientConfig(base_url="http://localhost:8000")
    cfg.user_email = "ivan@acme.ru"
    saved: dict = {}
    _wire_code_login(monkeypatch, cfg, saved, sub="1")

    m._do_code_login(cfg, code="ABC")

    assert saved["email"] == "ivan@acme.ru"
    assert cfg.user_email == "ivan@acme.ru"


def test_password_login_rejects_garbage_email_before_network(monkeypatch) -> None:
    """Мусор отсекается ДО сетевого вызова (клиент даже не создаётся)."""
    cfg = ClientConfig(base_url="http://localhost:8000")
    called: list[str] = []
    monkeypatch.setattr(m, "HubClient", lambda **kw: called.append("client"))

    with pytest.raises(typer.Exit):
        m._do_password_login(cfg, email="1", password="secret")

    assert called == []


def test_set_tokens_rejects_garbage_email(monkeypatch) -> None:
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    saved: list = []
    monkeypatch.setattr(m, "save_tokens", lambda *a, **k: saved.append(a))

    with pytest.raises(typer.Exit):
        m.cmd_set_tokens(email="1", access="a", refresh="r", base_url=None)

    assert saved == []


# --------------------------------------------------------------------------- #
# save_tokens: половинчатую сессию не сохраняем
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "email,access,refresh",
    [("u@e.io", "acc", ""), ("u@e.io", "", "ref"), ("", "acc", "ref")],
)
def test_save_tokens_refuses_partial_pair(email, access, refresh) -> None:
    from skillery_cli import config as config_mod

    with pytest.raises(RuntimeError, match="наполовину"):
        config_mod.save_tokens(email, access, refresh)


# --------------------------------------------------------------------------- #
# Протухшая сессия: понятный текст вместо «401 Signature has expired»
# --------------------------------------------------------------------------- #
class TestExpiredSessionMessage:
    def _raise_401(self):
        async def _coro() -> None:
            raise ApiError(
                status_code=401, code="UNKNOWN", message="Signature has expired"
            )

        return _coro()

    def test_run_explains_expired_session(self, monkeypatch, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            m._run(self._raise_401())
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Сессия истекла" in out
        assert "login" in out

    def test_run_reports_refresh_reason(self, monkeypatch, capsys) -> None:
        """Если авто-refresh не сработал — причина видна в том же сообщении."""
        m._REFRESH_FAILURE["reason"] = "refresh-токен для u@e.io не найден в хранилище"
        with pytest.raises(SystemExit):
            m._run(self._raise_401())
        out = capsys.readouterr().out
        assert "не найден в хранилище" in out

    async def test_refresh_callback_records_missing_refresh(self, monkeypatch) -> None:
        cfg = ClientConfig(base_url="http://localhost:8000")
        cfg.user_email = "u@e.io"
        monkeypatch.setattr(m, "load_tokens", lambda email: ("acc", None))

        cb = m._make_refresh_callback(cfg)
        assert await cb() is None  # type: ignore[operator]
        assert "refresh-токен" in (m._REFRESH_FAILURE["reason"] or "")

    async def test_refresh_callback_records_server_rejection(self, monkeypatch) -> None:
        cfg = ClientConfig(base_url="http://localhost:8000")
        cfg.user_email = "u@e.io"
        monkeypatch.setattr(m, "load_tokens", lambda email: ("acc", "ref"))

        class _Sub:
            async def refresh(self, token: str):
                raise ApiError(status_code=401, code="TOKEN_REVOKED", message="нет")

            async def close(self) -> None:
                return None

        monkeypatch.setattr(m, "HubClient", lambda **kw: _Sub())
        cb = m._make_refresh_callback(cfg)
        assert await cb() is None  # type: ignore[operator]
        assert "TOKEN_REVOKED" in (m._REFRESH_FAILURE["reason"] or "")

    def test_get_access_token_flags_missing_refresh(self, monkeypatch) -> None:
        cfg = ClientConfig(base_url="http://localhost:8000")
        cfg.user_email = "u@e.io"
        monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
        monkeypatch.setattr(m, "load_tokens", lambda email: ("acc", None))

        assert m._get_access_token() == "acc"
        assert m._REFRESH_FAILURE["reason"], "отсутствие refresh не помечено"
