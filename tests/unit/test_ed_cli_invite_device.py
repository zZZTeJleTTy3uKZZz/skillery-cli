"""E-D CLI-хвосты: одноразовый invite accept + регистрация устройства.

Контракты сверены с backend:

- ``POST /invites/accept`` (routes/invites.py:227) — body = ``{token}``,
  auth (Bearer), 204 No Content; 404 NOT_FOUND (невалидный токен);
  409 INVITE_UNUSABLE (исчерпан/отозван); 422 VALIDATION.
- ``POST /me/devices`` (routes/me.py:992) — body = ``{name, platform}``,
  auth, 201 ``DeviceDTO`` (upsert по (user, name)).

Отличие от ``join``/``accept_invite_link``: тот про ПЕРЕИСПОЛЬЗУЕМУЮ ссылку
(``/invite-links/accept``); здесь — про ОДНОРАЗОВЫЙ invite (``/invites/accept``),
выданный ``member invite``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import typer

from skillery_cli import output as output_module
from skillery_cli.commands import _common
from skillery_cli.commands import account as account_mod
from skillery_cli.config import ClientConfig


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


def _fake_logged_in_client(
    monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock
) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


# ==================== transport: accept_invite (одноразовый) ==============
@pytest.mark.asyncio
async def test_hubclient_accept_invite_posts_token_with_auth() -> None:
    import json as _json

    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/invites/accept").mock(return_value=Response(204))
        client = HubClient(base_url="http://localhost:8000", access_token="acc")
        try:
            result = await client.accept_invite(token="INVITETOKEN12345678")
        finally:
            await client.close()
        assert route.called
        sent = _json.loads(route.calls[0].request.content)
        assert sent == {"token": "INVITETOKEN12345678"}
        assert result is None
        assert route.calls[0].request.headers["Authorization"] == "Bearer acc"


# ==================== transport: register_device ==========================
@pytest.mark.asyncio
async def test_hubclient_register_device_posts_name_platform() -> None:
    import json as _json

    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/me/devices").mock(
            return_value=Response(
                201,
                json={
                    "id": "1",
                    "name": "laptop",
                    "platform": "linux",
                    "last_seen_at": "2026-07-08T00:00:00Z",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="acc")
        try:
            result = await client.register_device(
                name="laptop", platform="linux"
            )
        finally:
            await client.close()
        assert route.called
        sent = _json.loads(route.calls[0].request.content)
        assert sent == {"name": "laptop", "platform": "linux"}
        assert result["name"] == "laptop"
        assert route.calls[0].request.headers["Authorization"] == "Bearer acc"


# ==================== cmd_accept_invite: happy path =======================
def test_cmd_accept_invite_logged_in_accepts_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_module._mode = "text"
    _patch_cfg(
        monkeypatch, user_email="u@example.com", permissions=["skill.read"]
    )

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _accept(*, token: str) -> None:
        captured["token"] = token
        return None

    async def _close() -> None:
        return None

    fake_client.accept_invite = _accept
    fake_client.close = _close
    _fake_logged_in_client(monkeypatch, fake_client)

    account_mod.cmd_accept_invite(token="INVITETOKEN12345678", base_url=None)
    assert captured["token"] == "INVITETOKEN12345678"


def test_cmd_accept_invite_requires_login(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Не залогинен → exit 1 (одноразовый invite принимает только юзер)."""
    output_module._mode = "text"
    _patch_cfg(monkeypatch)  # без user_email

    def _boom(**kw: Any) -> None:
        raise AssertionError("HTTP-клиент не должен создаваться без логина")

    monkeypatch.setattr(_common, "HubClient", _boom)

    with pytest.raises(typer.Exit) as exc:
        account_mod.cmd_accept_invite(
            token="INVITETOKEN12345678", base_url=None
        )
    assert exc.value.exit_code == 1


def test_cmd_accept_invite_unusable_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """409 INVITE_UNUSABLE → читабельная ошибка + exit 1."""
    from skillery_cli.core.transport import ApiError

    output_module._mode = "text"
    _patch_cfg(
        monkeypatch, user_email="u@example.com", permissions=["skill.read"]
    )

    fake_client = MagicMock()

    async def _accept(*, token: str) -> None:
        raise ApiError(409, "INVITE_UNUSABLE", "Инвайт исчерпан", {})

    async def _close() -> None:
        return None

    fake_client.accept_invite = _accept
    fake_client.close = _close
    _fake_logged_in_client(monkeypatch, fake_client)

    with pytest.raises(SystemExit) as exc:
        account_mod.cmd_accept_invite(
            token="INVITETOKEN12345678", base_url=None
        )
    assert exc.value.code == 1
    assert "INVITE_UNUSABLE" in capsys.readouterr().out


# ==================== register_device_best_effort =========================
def test_local_device_identity_shape() -> None:
    name, plat, cdid = _common.local_device_identity()
    assert isinstance(name, str) and name
    assert isinstance(plat, str) and plat
    assert plat == plat.lower()
    assert isinstance(cdid, str) and cdid  # стабильный client_device_id


@pytest.mark.asyncio
async def test_register_device_best_effort_calls_client() -> None:
    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _reg(
        *, name: str, platform: str, client_device_id: str | None = None
    ) -> dict[str, Any]:
        captured.update(
            {"name": name, "platform": platform, "client_device_id": client_device_id}
        )
        return {"id": "1", "name": name, "platform": platform}

    fake_client.register_device = _reg
    await _common.register_device_best_effort(fake_client)
    assert captured["name"]
    assert captured["platform"]
    assert captured["client_device_id"]  # стабильный id должен прокидываться


@pytest.mark.asyncio
async def test_register_device_best_effort_swallows_api_error() -> None:
    """Старый backend без /me/devices / сеть → login не валим."""
    from skillery_cli.core.transport import ApiError

    fake_client = MagicMock()

    async def _reg(**kw: Any) -> dict[str, Any]:
        raise ApiError(404, "NOT_FOUND", "нет такого маршрута", {})

    fake_client.register_device = _reg
    # не должно бросить
    await _common.register_device_best_effort(fake_client)


@pytest.mark.asyncio
async def test_register_device_best_effort_swallows_network_error() -> None:
    fake_client = MagicMock()

    async def _reg(**kw: Any) -> dict[str, Any]:
        raise OSError("connection refused")

    fake_client.register_device = _reg
    await _common.register_device_best_effort(fake_client)


# ==================== build_app: команда присутствует =====================
def test_accept_invite_command_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cfg(monkeypatch)
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [c.name for c in app.registered_commands]
    assert "accept-invite" in names
