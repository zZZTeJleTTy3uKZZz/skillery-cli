"""Тесты ``skillery ticket`` / ``tickets`` (E23 / E8)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response

from skillery_cli import output as output_module
from skillery_cli.commands import _common, ticket as ticket_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import HubClient


def _text_mode() -> None:
    output_module._mode = "text"


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


# ---------------------- HubClient ----------------------
@pytest.mark.asyncio
async def test_hubclient_create_ticket() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/support/tickets").mock(
            return_value=Response(
                201,
                json={
                    "id": "tkt_x",
                    "company_id": "cmp_y",
                    "creator_id": "u1",
                    "assignee_id": None,
                    "skill_id": None,
                    "kind": "bug",
                    "priority": "high",
                    "status": "open",
                    "subject": "broken",
                    "body": "details",
                    "screenshots": [],
                    "created_at": "2026-05-26T12:00:00Z",
                    "updated_at": "2026-05-26T12:00:00Z",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.create_ticket(
                subject="broken", body="details", kind="bug", priority="high"
            )
        finally:
            await client.close()
        assert route.called
        body = route.calls[0].request.read()
        assert b'"kind": "bug"' in body or b'"kind":"bug"' in body
        assert r["status"] == "open"


@pytest.mark.asyncio
async def test_hubclient_list_tickets_passes_filters() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/support/tickets").mock(
            return_value=Response(
                200,
                json={
                    "items": [],
                    "total": 0,
                    "page": 1,
                    "page_size": 50,
                    "has_more": False,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.list_tickets(status="open", kind="bug", page=2)
        finally:
            await client.close()
        params = dict(route.calls[0].request.url.params)
        assert params.get("status") == "open"
        assert params.get("kind") == "bug"
        assert params.get("page") == "2"


# ---------------------- cmd_ticket_create ----------------------
def test_cmd_ticket_create_invalid_kind() -> None:
    _text_mode()
    import typer

    with pytest.raises(typer.Exit) as exc:
        ticket_mod.cmd_ticket_create(
            subject="x", body="", skill=None, kind="invalid-kind", priority="normal"
        )
    assert exc.value.exit_code == 1


def test_cmd_ticket_create_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["ticket.create", "skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _resolve(client, slug):  # noqa: ANN001
        return "slk_x"

    monkeypatch.setattr(_common, "resolve_skill_id", _resolve)

    async def _create(*, subject, body, kind, priority, skill_id):  # noqa: ANN001
        captured.update(
            {
                "subject": subject,
                "body": body,
                "kind": kind,
                "priority": priority,
                "skill_id": skill_id,
            }
        )
        return {
            "id": "tkt_z",
            "company_id": "cmp_y",
            "creator_id": "u1",
            "assignee_id": None,
            "skill_id": skill_id,
            "kind": kind,
            "priority": priority,
            "status": "open",
            "subject": subject,
            "body": body,
            "screenshots": [],
            "created_at": "2026-05-26T12:00:00Z",
            "updated_at": "2026-05-26T12:00:00Z",
        }

    async def _close() -> None:
        return None

    fake_client.create_ticket = _create
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    ticket_mod.cmd_ticket_create(
        subject="Bug",
        body="Detailed body",
        skill="my-skill",
        kind="bug",
        priority="normal",
    )
    assert captured["subject"] == "Bug"
    assert captured["skill_id"] == "slk_x"
    assert captured["kind"] == "bug"


# ---------------------- cmd_tickets_list ----------------------
def test_cmd_tickets_list_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["ticket.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _list(
        *,
        status=None,
        kind=None,
        priority=None,
        skill_id=None,
        page=1,
        page_size=50,
    ):  # noqa: ANN001
        captured.update(
            {
                "status": status,
                "kind": kind,
                "page": page,
                "page_size": page_size,
            }
        )
        return {
            "items": [
                {
                    "id": "tkt_1",
                    "status": "open",
                    "kind": "bug",
                    "priority": "high",
                    "subject": "T1",
                }
            ],
            "total": 1,
            "page": page,
            "page_size": page_size,
            "has_more": False,
        }

    async def _close() -> None:
        return None

    fake_client.list_tickets = _list
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    ticket_mod.cmd_tickets_list(
        status_="open",
        kind="bug",
        priority=None,
        skill=None,
        page=1,
        page_size=50,
    )
    assert captured["status"] == "open"


# ---------------------- registration ----------------------
def test_ticket_subapps_registered_when_perm(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["ticket.create"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    typer_names = [t.name for t in app.registered_groups]
    assert "ticket" in typer_names
    assert "tickets" in typer_names
