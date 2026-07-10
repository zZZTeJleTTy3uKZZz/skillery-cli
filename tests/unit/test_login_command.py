"""Тесты команды `skillery login` (invite-flow + email/password flow)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as output_module
from skillery_cli.config import ClientConfig


def _patch_output_text_mode() -> None:
    output_module._mode = "text"


# ---------------------- HubClient.login_password ----------------------
@pytest.mark.asyncio
async def test_hubclient_login_password_posts_correct_endpoint() -> None:
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/auth/login").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "acc",
                    "refresh_token": "ref",
                    "user_id": "u1",
                    "access_expires_at": "2026-05-26T12:00:00Z",
                    "refresh_expires_at": "2026-06-26T12:00:00Z",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.login_password(
                email="ivan@acme.ru", password="supersecret"
            )
        finally:
            await client.close()
        assert route.called
        assert result["access_token"] == "acc"
        assert result["refresh_token"] == "ref"


# ---------------------- HubClient.get_me_permissions ----------------------
@pytest.mark.asyncio
async def test_hubclient_get_me_permissions_returns_list() -> None:
    """JWT-slim: CLI берёт эффективные права из /me/permissions (БД-авторитетно),
    а не из JWT-claim. Метод возвращает только список permission-ключей."""
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/me/permissions").mock(
            return_value=Response(
                200,
                json={
                    "permissions": ["skill.install", "skill.read"],
                    "company": None,
                    "role": None,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        client.set_access_token("acc-token")
        try:
            perms = await client.get_me_permissions()
        finally:
            await client.close()
        assert route.called
        # Bearer проставлен set_access_token'ом.
        assert route.calls.last.request.headers["Authorization"] == "Bearer acc-token"
        assert perms == ["skill.install", "skill.read"]


# ---------------------- login --email --password flow ----------------------
def test_login_password_flow_calls_endpoint_and_saves_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skillery login --email X --password Y (без invite) → POST /auth/login."""
    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()

    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    saved: dict[str, Any] = {}
    monkeypatch.setattr(
        main_mod,
        "save_tokens",
        lambda email, access, refresh: saved.update(
            {"email": email, "access": access, "refresh": refresh}
        ),
    )
    monkeypatch.setattr(main_mod, "populate_from_jwt", lambda c, t: None)
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)

    fake_client = MagicMock()

    async def _fake_login_password(*, email: str, password: str) -> dict[str, Any]:
        assert email == "ivan@acme.ru"
        assert password == "supersecret"
        return {
            "access_token": "fake-access",
            "refresh_token": "fake-refresh",
            "user_id": "u-1",
            "access_expires_at": "2026-05-26T12:00:00Z",
            "refresh_expires_at": "2026-06-26T12:00:00Z",
        }

    async def _fake_close() -> None:
        return None

    async def _fake_get_me_permissions() -> list[str]:
        return ["skill.read"]

    fake_client.login_password = _fake_login_password
    fake_client.close = _fake_close
    fake_client.get_me_permissions = _fake_get_me_permissions
    monkeypatch.setattr(main_mod, "HubClient", lambda **kw: fake_client)

    main_mod.cmd_login(
        invite=None,
        email="ivan@acme.ru",
        name=None,
        password="supersecret",
        base_url=None,
    )

    assert saved["email"] == "ivan@acme.ru"
    assert saved["access"] == "fake-access"
    assert saved["refresh"] == "fake-refresh"
    # JWT-slim: права пришли из /me/permissions, не из токена.
    assert cfg.permissions == ["skill.read"]


def test_login_with_invite_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backwards compat: skillery login <invite-token> --email X --name Y."""
    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "save_tokens", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "populate_from_jwt", lambda *a, **k: None)
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)

    fake_client = MagicMock()

    invoked: dict[str, Any] = {}

    async def _fake_invite(
        *, invite_token: str, email: str, display_name: str
    ) -> dict[str, Any]:
        invoked.update(
            {"invite_token": invite_token, "email": email, "name": display_name}
        )
        return {"access_token": "a", "refresh_token": "r"}

    async def _fake_close() -> None:
        return None

    async def _fake_get_me_permissions() -> list[str]:
        return []

    fake_client.login_invite = _fake_invite
    fake_client.close = _fake_close
    fake_client.get_me_permissions = _fake_get_me_permissions
    monkeypatch.setattr(main_mod, "HubClient", lambda **kw: fake_client)

    main_mod.cmd_login(
        invite="ABCDEFGHIJKLMNOPQRSTUVWX",
        email="ivan@acme.ru",
        name="Иван",
        password=None,
        base_url=None,
    )

    assert invoked == {
        "invite_token": "ABCDEFGHIJKLMNOPQRSTUVWX",
        "email": "ivan@acme.ru",
        "name": "Иван",
    }


def test_login_password_in_json_mode_requires_email_and_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В JSON-режиме без --email или --password выходит с code=1."""
    import typer

    from skillery_cli import __main__ as main_mod

    output_module._mode = "json"
    try:
        cfg = ClientConfig(base_url="http://localhost:8000")
        monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
        # no email
        with pytest.raises(typer.Exit) as exc:
            main_mod.cmd_login(
                invite=None,
                email=None,
                name=None,
                password="x" * 9,
                base_url=None,
            )
        assert exc.value.exit_code == 1
        # no password
        with pytest.raises(typer.Exit) as exc2:
            main_mod.cmd_login(
                invite=None,
                email="a@b.co",
                name=None,
                password=None,
                base_url=None,
            )
        assert exc2.value.exit_code == 1
    finally:
        output_module._mode = "text"


# ---------------------- HubClient.exchange_redeem ----------------------
@pytest.mark.asyncio
async def test_hubclient_exchange_redeem_posts_correct_endpoint() -> None:
    """exchange_redeem POST /auth/exchanges/{code}/redeem с include_refresh=true."""
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/auth/exchanges/ABC123/redeem").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "user_id": "u1",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.exchange_redeem(code="ABC123")
        finally:
            await client.close()
        assert route.called
        # Проверим, что body содержит {"code": "ABC123", "include_refresh": true}
        req = route.calls.last.request
        import json as json_mod
        body = json_mod.loads(req.content)
        assert body["code"] == "ABC123"
        assert body["include_refresh"] is True
        assert result["access_token"] == "new-access"
        assert result["refresh_token"] == "new-refresh"


# ---------------------- cmd_login --code <str> flow ----------------------
def test_login_with_code_flag_redeems_and_saves_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skillery login --code ABC123 → direct redeem without browser."""
    from skillery_cli import __main__ as main_mod
    from skillery_cli.config import decode_jwt_claims as orig_decode

    _patch_output_text_mode()

    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    saved: dict[str, Any] = {}
    monkeypatch.setattr(
        main_mod,
        "save_tokens",
        lambda email, access, refresh: saved.update(
            {"email": email, "access": access, "refresh": refresh}
        ),
    )
    monkeypatch.setattr(main_mod, "populate_from_jwt", lambda c, t: None)
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)

    fake_client = MagicMock()

    async def _fake_exchange_redeem(*, code: str) -> dict[str, Any]:
        assert code == "ABC123"
        return {
            "access_token": "fake-access-from-code",
            "refresh_token": "fake-refresh-from-code",
        }

    async def _fake_close() -> None:
        return None

    async def _fake_get_me_permissions() -> list[str]:
        return ["skill.read"]

    fake_client.exchange_redeem = _fake_exchange_redeem
    fake_client.close = _fake_close
    fake_client.get_me_permissions = _fake_get_me_permissions
    monkeypatch.setattr(main_mod, "HubClient", lambda **kw: fake_client)

    # Мокируем decode_jwt_claims чтобы вернуть фейковый email (в реальности из JWT)
    monkeypatch.setattr(
        main_mod, "decode_jwt_claims", lambda token: {"sub": "user@example.com"}
    )

    main_mod.cmd_login(
        invite=None,
        email=None,
        name=None,
        password=None,
        code="ABC123",
        base_url=None,
    )

    assert saved["email"] == "user@example.com"
    assert saved["access"] == "fake-access-from-code"
    assert saved["refresh"] == "fake-refresh-from-code"


# ---------------------- Browser-flow logic ----------------------
def test_login_browser_flow_state_mismatch_rejected() -> None:
    """Browser-flow callback: state-mismatch → ошибка."""
    from skillery_cli.core.login_helpers import validate_callback_state

    expected_state = "abcdef1234567890"
    actual_state = "wrongstate1234567890"

    with pytest.raises(ValueError, match="state mismatch"):
        validate_callback_state(expected=expected_state, actual=actual_state)
