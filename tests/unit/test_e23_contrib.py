"""Тесты ``skillery contributors`` (E23 / E7)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response

from skillery_cli import output as output_module
from skillery_cli.commands import _common, contrib as contrib_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import HubClient


def _text_mode() -> None:
    output_module._mode = "text"


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


@pytest.mark.asyncio
async def test_hubclient_list_contributors_default() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills/slk_x/contributors").mock(
            return_value=Response(200, json={"object": "list", "data": []})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.list_contributors("slk_x")
        finally:
            await client.close()
        params = dict(route.calls[0].request.url.params)
        assert params.get("refresh") == "false"


@pytest.mark.asyncio
async def test_hubclient_list_contributors_refresh_true() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills/slk_x/contributors").mock(
            return_value=Response(200, json={"object": "list", "data": []})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.list_contributors("slk_x", refresh=True)
        finally:
            await client.close()
        params = dict(route.calls[0].request.url.params)
        assert params.get("refresh") == "true"


def test_cmd_contributors_renders_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _resolve(client, slug):  # noqa: ANN001
        return "slk_y"

    monkeypatch.setattr(_common, "resolve_skill_id", _resolve)

    async def _contribs(skill_id: str, *, refresh: bool = False) -> dict[str, Any]:
        captured["skill_id"] = skill_id
        captured["refresh"] = refresh
        return {
            "object": "list",
            "data": [
                {
                    "email": "alice@example.com",
                    "display_name": "Alice",
                    "commit_count": 12,
                    "last_commit_at": "2026-04-12T00:00:00Z",
                },
                {
                    "email": "bob@example.com",
                    "display_name": "Bob",
                    "commit_count": 3,
                    "last_commit_at": "2026-04-15T00:00:00Z",
                },
            ],
        }

    async def _close() -> None:
        return None

    fake_client.list_contributors = _contribs
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    contrib_mod.cmd_contributors(slug="my-skill", refresh=False)
    assert captured["skill_id"] == "slk_y"
    assert captured["refresh"] is False
