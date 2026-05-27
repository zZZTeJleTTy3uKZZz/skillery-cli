"""Тесты команды `skills-hub login` (invite-flow + email/password flow)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from skills_hub_cli import output as output_module
from skills_hub_cli.config import ClientConfig


def _patch_output_text_mode() -> None:
    output_module._mode = "text"


# ---------------------- HubClient.login_password ----------------------
@pytest.mark.asyncio
async def test_hubclient_login_password_posts_correct_endpoint() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

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


# ---------------------- login --email --password flow ----------------------
def test_login_password_flow_calls_endpoint_and_saves_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skills-hub login --email X --password Y (без invite) → POST /auth/login."""
    from skills_hub_cli import __main__ as main_mod

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

    fake_client.login_password = _fake_login_password
    fake_client.close = _fake_close
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


def test_login_with_invite_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backwards compat: skills-hub login <invite-token> --email X --name Y."""
    from skills_hub_cli import __main__ as main_mod

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

    fake_client.login_invite = _fake_invite
    fake_client.close = _fake_close
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

    from skills_hub_cli import __main__ as main_mod

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
