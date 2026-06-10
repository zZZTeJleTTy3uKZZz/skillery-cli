"""Тесты ``skills-hub ticket reply`` + ``ticket status`` (P0).

Статусы сверены с доменом ``TicketStatus`` (backend):
``new | in_progress | scheduled | done | rejected``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from skills_hub_cli import output as output_module
from skills_hub_cli.commands import _common, ticket as ticket_mod
from skills_hub_cli.config import ClientConfig


def _text_mode() -> None:
    output_module._mode = "text"


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


def _cfg(monkeypatch: pytest.MonkeyPatch, perms: list[str]) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=perms,
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))


# ==================== ticket reply ====================
def test_cmd_ticket_reply_json_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    _cfg(monkeypatch, ["ticket.create"])

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _reply(ticket_id: str, *, body: str, parent_id: str | None = None) -> dict:
        captured.update({"ticket_id": ticket_id, "body": body, "parent_id": parent_id})
        return {
            "message": {
                "id": "msg_1",
                "ticket_id": ticket_id,
                "author_id": "u1",
                "body": body,
                "parent_id": parent_id,
                "screenshots": [],
                "created_at": "2026-06-10T12:00:00Z",
                "is_deleted": False,
            }
        }

    async def _reply_mp(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("multipart не должен звониться без скриншота")

    async def _close() -> None:
        return None

    fake_client.reply_ticket = _reply
    fake_client.reply_ticket_multipart = _reply_mp
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    ticket_mod.cmd_ticket_reply(
        ticket_id="42", body="any update?", parent_id=None, screenshot=None
    )
    assert captured["ticket_id"] == "42"
    assert captured["body"] == "any update?"


def test_cmd_ticket_reply_multipart_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    _cfg(monkeypatch, ["ticket.create"])

    shot = tmp_path / "err.png"
    shot.write_bytes(b"\x89PNG\r\nbin")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _reply(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("JSON не должен звониться со скриншотом")

    async def _reply_mp(ticket_id: str, *, body: str, screenshots) -> dict:  # noqa: ANN001
        captured["files"] = [n for n, _ in screenshots]
        captured["ticket_id"] = ticket_id
        return {
            "message": {
                "id": "msg_mp",
                "ticket_id": ticket_id,
                "author_id": "u1",
                "body": body,
                "parent_id": None,
                "screenshots": ["/uploads/tickets/42/0_err.png"],
                "created_at": "2026-06-10T12:00:00Z",
                "is_deleted": False,
            }
        }

    async def _close() -> None:
        return None

    fake_client.reply_ticket = _reply
    fake_client.reply_ticket_multipart = _reply_mp
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    ticket_mod.cmd_ticket_reply(
        ticket_id="42", body="see attached", parent_id=None, screenshot=[shot]
    )
    assert captured["files"] == ["err.png"]
    assert captured["ticket_id"] == "42"


def test_cmd_ticket_reply_rejects_empty_body() -> None:
    _text_mode()
    import typer

    with pytest.raises(typer.Exit) as exc:
        ticket_mod.cmd_ticket_reply(
            ticket_id="42", body="   ", parent_id=None, screenshot=None
        )
    assert exc.value.exit_code == 1


def test_cmd_ticket_reply_missing_screenshot_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    import typer

    _cfg(monkeypatch, ["ticket.create"])
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))

    missing = tmp_path / "nope.png"
    with pytest.raises(typer.Exit) as exc:
        ticket_mod.cmd_ticket_reply(
            ticket_id="42", body="hi", parent_id=None, screenshot=[missing]
        )
    assert exc.value.exit_code == 1


# ==================== ticket status ====================
def test_cmd_ticket_status_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    _cfg(monkeypatch, ["ticket.update_status"])

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _set(ticket_id: str, *, status: str) -> dict:
        captured.update({"ticket_id": ticket_id, "status": status})
        return {
            "id": ticket_id,
            "company_id": "1",
            "creator_id": "u1",
            "assignee_id": None,
            "skill_id": None,
            "kind": "bug",
            "priority": "normal",
            "status": status,
            "subject": "S",
            "body": "B",
            "screenshots": [],
            "created_at": "2026-06-10T12:00:00Z",
            "updated_at": "2026-06-10T12:05:00Z",
        }

    async def _close() -> None:
        return None

    fake_client.set_ticket_status = _set
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    ticket_mod.cmd_ticket_status(ticket_id="42", status="in_progress")
    assert captured["ticket_id"] == "42"
    assert captured["status"] == "in_progress"


def test_cmd_ticket_status_rejects_invalid_status() -> None:
    """Старые значения (open/resolved/closed) — отвергаются ДО HTTP."""
    _text_mode()
    import typer

    for bad in ("open", "resolved", "closed", "reopened", "bogus"):
        with pytest.raises(typer.Exit) as exc:
            ticket_mod.cmd_ticket_status(ticket_id="42", status=bad)
        assert exc.value.exit_code == 1


def test_cmd_ticket_status_accepts_all_real_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Все 5 валидных backend-статусов проходят валидацию."""
    _text_mode()
    _cfg(monkeypatch, ["ticket.update_status"])

    fake_client = MagicMock()

    async def _set(ticket_id: str, *, status: str) -> dict:
        return {
            "id": ticket_id, "company_id": "1", "creator_id": "u1",
            "assignee_id": None, "skill_id": None, "kind": "bug",
            "priority": "normal", "status": status, "subject": "S", "body": "B",
            "screenshots": [], "created_at": "2026-06-10T12:00:00Z",
            "updated_at": "2026-06-10T12:05:00Z",
        }

    async def _close() -> None:
        return None

    fake_client.set_ticket_status = _set
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    for ok in ("new", "in_progress", "scheduled", "done", "rejected"):
        ticket_mod.cmd_ticket_status(ticket_id="42", status=ok)


# ==================== registration ====================
def test_ticket_reply_status_registered_with_update_perm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["ticket.create", "ticket.update_status"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    ticket_group = next(t for t in app.registered_groups if t.name == "ticket")
    sub_names = [c.name for c in ticket_group.typer_instance.registered_commands]
    assert "reply" in sub_names
    assert "status" in sub_names


def test_ticket_reply_present_status_absent_without_update_perm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reply нужен только ticket.create; status нужен ticket.update_status."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["ticket.create"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    ticket_group = next(t for t in app.registered_groups if t.name == "ticket")
    sub_names = [c.name for c in ticket_group.typer_instance.registered_commands]
    assert "reply" in sub_names
    assert "status" not in sub_names
