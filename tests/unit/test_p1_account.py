"""Тесты P1 C1 ``account``: register / join (E6).

Контракты сверены с backend:

- ``POST /auth/register`` (routes/auth.py:137) — body =
  ``{email, password, display_name}`` (display_name ОБЯЗАТЕЛЕН на бэке —
  schemas: min_length=1 → CLI дефолтит из local-part email), 201
  ``LoginPasswordResponse``; 409 EMAIL_TAKEN; 422 VALIDATION (пароль < 8).
- ``POST /auth/register-with-link`` (routes/auth.py:188) — body =
  ``{email, password, display_name, token}``, 201 ``LoginPasswordResponse``;
  404 LINK_NOT_FOUND; 409 CONFLICT (email занят / ссылка исчерпана);
  422 VALIDATION.
- ``POST /invite-links/accept`` (routes/company_invite_links.py:207) —
  body = ``{token}``, auth required, 204 No Content; 404 NOT_FOUND;
  409 LINK_UNUSABLE.

Join-URL: web строит ссылку как ``{origin}/join/{token}``
(web/src/widgets/company-invite-links) — CLI вычленяет последний
path-сегмент.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import typer

from skills_hub_cli import output as output_module
from skills_hub_cli.commands import _common
from skills_hub_cli.commands import account as account_mod
from skills_hub_cli.config import ClientConfig


async def _no_perms() -> list[str]:
    """JWT-slim: register/join после логина дёргают /me/permissions —
    fake-клиенту нужен async-метод, иначе await падает на MagicMock."""
    return []


def _text_mode() -> None:
    output_module._mode = "text"


def _json_mode() -> None:
    output_module._mode = "json"


@pytest.fixture(autouse=True)
def _restore_text_mode():
    yield
    output_module._mode = "text"


def _patch_cfg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_email: str | None = None,
    permissions: list[str] | None = None,
) -> ClientConfig:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email=user_email,
        permissions=permissions or [],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    return cfg


def _patch_session_io(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Мокает save_tokens/populate_from_jwt в неймспейсе account-модуля."""
    saved: dict[str, Any] = {}
    monkeypatch.setattr(
        account_mod,
        "save_tokens",
        lambda email, access, refresh: saved.update(
            {"email": email, "access": access, "refresh": refresh}
        ),
    )
    monkeypatch.setattr(account_mod, "populate_from_jwt", lambda c, t: None)
    return saved


def _no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """HubClient не должен создаваться вовсе (валидация ДО сети)."""

    def _boom(**kw: Any) -> None:
        raise AssertionError("HTTP-клиент не должен создаваться")

    monkeypatch.setattr(_common, "HubClient", _boom)


def _token_pair_response(user_id: str = "7") -> dict[str, Any]:
    return {
        "access_token": "fake-access",
        "refresh_token": "fake-refresh",
        "access_expires_at": "2026-06-11T12:00:00Z",
        "refresh_expires_at": "2026-07-11T12:00:00Z",
        "user_id": user_id,
    }


# ==================== transport: HubClient методы ====================
@pytest.mark.asyncio
async def test_hubclient_register_posts_to_auth_register() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/auth/register").mock(
            return_value=Response(201, json=_token_pair_response())
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.register(
                email="new@acme.ru", password="supersecret", display_name="Новичок"
            )
        finally:
            await client.close()
        assert route.called
        import json as _json

        sent = _json.loads(route.calls[0].request.content)
        assert sent == {
            "email": "new@acme.ru",
            "password": "supersecret",
            "display_name": "Новичок",
        }
        assert result["access_token"] == "fake-access"
        assert result["refresh_token"] == "fake-refresh"


@pytest.mark.asyncio
async def test_hubclient_register_with_link_posts_token() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/auth/register-with-link").mock(
            return_value=Response(201, json=_token_pair_response())
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.register_with_link(
                email="new@acme.ru",
                password="supersecret",
                display_name="Новичок",
                token="JOINTOKEN123",
            )
        finally:
            await client.close()
        assert route.called
        import json as _json

        sent = _json.loads(route.calls[0].request.content)
        assert sent == {
            "email": "new@acme.ru",
            "password": "supersecret",
            "display_name": "Новичок",
            "token": "JOINTOKEN123",
        }
        assert result["access_token"] == "fake-access"


@pytest.mark.asyncio
async def test_hubclient_accept_invite_link_returns_none_on_204() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/invite-links/accept").mock(
            return_value=Response(204)
        )
        client = HubClient(
            base_url="http://localhost:8000", access_token="acc"
        )
        try:
            result = await client.accept_invite_link(token="JOINTOKEN123")
        finally:
            await client.close()
        assert route.called
        import json as _json

        sent = _json.loads(route.calls[0].request.content)
        assert sent == {"token": "JOINTOKEN123"}
        assert result is None
        assert (
            route.calls[0].request.headers["Authorization"] == "Bearer acc"
        )


# ==================== _strip_join_url ====================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://hub.example/join/AbC-12_3", "AbC-12_3"),
        ("https://hub.example/join/AbC-12_3/", "AbC-12_3"),
        ("http://localhost:3000/join/tok99", "tok99"),
        ("/join/onlypath", "onlypath"),
        ("BARETOKEN123", "BARETOKEN123"),
        ("  https://hub.example/join/padded  ", "padded"),
    ],
)
def test_strip_join_url(raw: str, expected: str) -> None:
    assert account_mod._strip_join_url(raw) == expected


# ==================== register: happy path ====================
def test_cmd_register_happy_path_saves_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _text_mode()
    cfg = _patch_cfg(monkeypatch)
    saved = _patch_session_io(monkeypatch)

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _register(
        *, email: str, password: str, display_name: str
    ) -> dict[str, Any]:
        captured.update(
            {"email": email, "password": password, "display_name": display_name}
        )
        return _token_pair_response()

    async def _close() -> None:
        return None

    fake_client.register = _register
    fake_client.close = _close
    fake_client.get_me_permissions = _no_perms
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)

    account_mod.cmd_register(
        email="ivan@acme.ru", password="supersecret", name="Иван", base_url=None
    )

    assert captured == {
        "email": "ivan@acme.ru",
        "password": "supersecret",
        "display_name": "Иван",
    }
    assert saved == {
        "email": "ivan@acme.ru",
        "access": "fake-access",
        "refresh": "fake-refresh",
    }
    assert cfg.user_email == "ivan@acme.ru"


def test_cmd_register_defaults_display_name_from_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Бэк требует display_name (min_length=1) — без --name берём local-part."""
    _text_mode()
    _patch_cfg(monkeypatch)
    _patch_session_io(monkeypatch)

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _register(
        *, email: str, password: str, display_name: str
    ) -> dict[str, Any]:
        captured["display_name"] = display_name
        return _token_pair_response()

    async def _close() -> None:
        return None

    fake_client.register = _register
    fake_client.close = _close
    fake_client.get_me_permissions = _no_perms
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)

    account_mod.cmd_register(
        email="petya@corp.io", password="supersecret", name=None, base_url=None
    )
    assert captured["display_name"] == "petya"


# ==================== register: валидация (до HTTP) ====================
def test_cmd_register_json_mode_requires_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _json_mode()
    _patch_cfg(monkeypatch)
    _no_http(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        account_mod.cmd_register(
            email=None, password="supersecret", name=None, base_url=None
        )
    assert exc.value.exit_code == 1


def test_cmd_register_json_mode_requires_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _json_mode()
    _patch_cfg(monkeypatch)
    _no_http(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        account_mod.cmd_register(
            email="a@b.co", password=None, name=None, base_url=None
        )
    assert exc.value.exit_code == 1


def test_cmd_register_rejects_short_password_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Бэк ответил бы 422 (min 8) — режем локально, HTTP не дёргаем."""
    _text_mode()
    _patch_cfg(monkeypatch)
    _no_http(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        account_mod.cmd_register(
            email="a@b.co", password="short7!", name=None, base_url=None
        )
    assert exc.value.exit_code == 1


# ==================== register: негатив 409 ====================
def test_cmd_register_email_taken_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """409 EMAIL_TAKEN от бэка → читабельная ошибка + exit 1."""
    from skills_hub_cli.core.transport import ApiError

    _text_mode()
    _patch_cfg(monkeypatch)
    _patch_session_io(monkeypatch)

    fake_client = MagicMock()

    async def _register(**kw: Any) -> dict[str, Any]:
        raise ApiError(409, "EMAIL_TAKEN", "Email уже зарегистрирован", {})

    async def _close() -> None:
        return None

    fake_client.register = _register
    fake_client.close = _close
    fake_client.get_me_permissions = _no_perms
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)

    with pytest.raises(SystemExit) as exc:
        account_mod.cmd_register(
            email="dup@acme.ru", password="supersecret", name=None, base_url=None
        )
    assert exc.value.code == 1
    assert "EMAIL_TAKEN" in capsys.readouterr().out


def test_cmd_register_api_error_json_mode_emits_error_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """В json-режиме ApiError → {"event":"error",...} в stderr, stdout чист."""
    import json as _json

    from skills_hub_cli.core.transport import ApiError

    _json_mode()
    _patch_cfg(monkeypatch)
    _patch_session_io(monkeypatch)

    fake_client = MagicMock()

    async def _register(**kw: Any) -> dict[str, Any]:
        raise ApiError(409, "EMAIL_TAKEN", "Email уже зарегистрирован", {})

    async def _close() -> None:
        return None

    fake_client.register = _register
    fake_client.close = _close
    fake_client.get_me_permissions = _no_perms
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)

    with pytest.raises(SystemExit) as exc:
        account_mod.cmd_register(
            email="dup@acme.ru", password="supersecret", name=None, base_url=None
        )
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    rec = _json.loads(captured.err.strip().splitlines()[-1])
    assert rec["event"] == "error"
    assert rec["code"] == "EMAIL_TAKEN"


# ==================== join: залогинен → accept ====================
def _fake_logged_in_client(
    monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock
) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


def test_cmd_join_logged_in_accepts_token_from_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _text_mode()
    _patch_cfg(
        monkeypatch, user_email="u@example.com", permissions=["skill.read"]
    )

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _accept(*, token: str) -> None:
        captured["token"] = token
        return None

    async def _register_with_link(**kw: Any) -> dict[str, Any]:
        raise AssertionError(
            "register-with-link не должен звониться у залогиненного"
        )

    async def _close() -> None:
        return None

    fake_client.accept_invite_link = _accept
    fake_client.register_with_link = _register_with_link
    fake_client.close = _close
    _fake_logged_in_client(monkeypatch, fake_client)

    account_mod.cmd_join(
        token_or_url="https://hub.example/join/JOINTOKEN123",
        email=None,
        password=None,
        name=None,
        base_url=None,
    )
    assert captured["token"] == "JOINTOKEN123"


def test_cmd_join_logged_in_link_unusable_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """409 LINK_UNUSABLE (исчерпана/отозвана) → exit 1."""
    from skills_hub_cli.core.transport import ApiError

    _text_mode()
    _patch_cfg(
        monkeypatch, user_email="u@example.com", permissions=["skill.read"]
    )

    fake_client = MagicMock()

    async def _accept(*, token: str) -> None:
        raise ApiError(409, "LINK_UNUSABLE", "Ссылка отозвана или исчерпана", {})

    async def _close() -> None:
        return None

    fake_client.accept_invite_link = _accept
    fake_client.close = _close
    _fake_logged_in_client(monkeypatch, fake_client)

    with pytest.raises(SystemExit) as exc:
        account_mod.cmd_join(
            token_or_url="JOINTOKEN123",
            email=None,
            password=None,
            name=None,
            base_url=None,
        )
    assert exc.value.code == 1
    assert "LINK_UNUSABLE" in capsys.readouterr().out


def test_cmd_join_logged_in_link_not_found_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 NOT_FOUND (невалидная ссылка) → exit 1."""
    from skills_hub_cli.core.transport import ApiError

    _text_mode()
    _patch_cfg(
        monkeypatch, user_email="u@example.com", permissions=["skill.read"]
    )

    fake_client = MagicMock()

    async def _accept(*, token: str) -> None:
        raise ApiError(404, "NOT_FOUND", "Ссылка не найдена", {})

    async def _close() -> None:
        return None

    fake_client.accept_invite_link = _accept
    fake_client.close = _close
    _fake_logged_in_client(monkeypatch, fake_client)

    with pytest.raises(SystemExit) as exc:
        account_mod.cmd_join(
            token_or_url="DEADTOKEN999",
            email=None,
            password=None,
            name=None,
            base_url=None,
        )
    assert exc.value.code == 1


# ==================== join: НЕ залогинен → register-with-link ====================
def test_cmd_join_not_logged_in_json_mode_requires_email_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """json-режим без --email/--password → VALIDATION, без интерактива и HTTP."""
    import json as _json

    _json_mode()
    _patch_cfg(monkeypatch)  # не залогинен
    _no_http(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        account_mod.cmd_join(
            token_or_url="JOINTOKEN123",
            email=None,
            password=None,
            name=None,
            base_url=None,
        )
    assert exc.value.exit_code == 1
    rec = _json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert rec["event"] == "error"
    assert rec["code"] == "VALIDATION"

    # --email есть, --password нет → тоже VALIDATION
    with pytest.raises(typer.Exit) as exc2:
        account_mod.cmd_join(
            token_or_url="JOINTOKEN123",
            email="a@b.co",
            password=None,
            name=None,
            base_url=None,
        )
    assert exc2.value.exit_code == 1


def test_cmd_join_not_logged_in_registers_with_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _text_mode()
    cfg = _patch_cfg(monkeypatch)
    saved = _patch_session_io(monkeypatch)

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _register_with_link(
        *, email: str, password: str, display_name: str, token: str
    ) -> dict[str, Any]:
        captured.update(
            {
                "email": email,
                "password": password,
                "display_name": display_name,
                "token": token,
            }
        )
        return _token_pair_response()

    async def _accept(**kw: Any) -> None:
        raise AssertionError("accept не должен звониться у НЕзалогиненного")

    async def _close() -> None:
        return None

    fake_client.register_with_link = _register_with_link
    fake_client.accept_invite_link = _accept
    fake_client.close = _close
    fake_client.get_me_permissions = _no_perms
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)

    account_mod.cmd_join(
        token_or_url="https://hub.example/join/JOINTOKEN123",
        email="new@acme.ru",
        password="supersecret",
        name="Новичок",
        base_url=None,
    )
    assert captured == {
        "email": "new@acme.ru",
        "password": "supersecret",
        "display_name": "Новичок",
        "token": "JOINTOKEN123",
    }
    assert saved["access"] == "fake-access"
    assert cfg.user_email == "new@acme.ru"


def test_cmd_join_register_with_link_invalid_link_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """404 LINK_NOT_FOUND при регистрации по мёртвой ссылке → exit 1."""
    from skills_hub_cli.core.transport import ApiError

    _text_mode()
    _patch_cfg(monkeypatch)
    _patch_session_io(monkeypatch)

    fake_client = MagicMock()

    async def _register_with_link(**kw: Any) -> dict[str, Any]:
        raise ApiError(404, "LINK_NOT_FOUND", "Ссылка не найдена", {})

    async def _close() -> None:
        return None

    fake_client.register_with_link = _register_with_link
    fake_client.close = _close
    fake_client.get_me_permissions = _no_perms
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)

    with pytest.raises(SystemExit) as exc:
        account_mod.cmd_join(
            token_or_url="DEADTOKEN999",
            email="new@acme.ru",
            password="supersecret",
            name=None,
            base_url=None,
        )
    assert exc.value.code == 1
    assert "LINK_NOT_FOUND" in capsys.readouterr().out


def test_cmd_join_not_logged_in_rejects_short_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _text_mode()
    _patch_cfg(monkeypatch)
    _no_http(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        account_mod.cmd_join(
            token_or_url="JOINTOKEN123",
            email="new@acme.ru",
            password="1234567",
            name=None,
            base_url=None,
        )
    assert exc.value.exit_code == 1


# ==================== build_app: always-on регистрация ====================
def test_register_and_join_present_when_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cfg(monkeypatch)  # без user_email → не залогинен
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    names = [c.name for c in app.registered_commands]
    assert "register" in names
    assert "join" in names


def test_register_and_join_present_when_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cfg(
        monkeypatch, user_email="u@example.com", permissions=["skill.read"]
    )
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    names = [c.name for c in app.registered_commands]
    assert "register" in names
    assert "join" in names
