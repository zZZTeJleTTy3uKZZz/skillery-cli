"""Тесты ``skillery comment-edit`` + ``comment-delete`` (P0).

Backend адресует comment по числовому id (``PATCH /comments/{id}`` /
``DELETE /comments/{id}``) — резолв скилла НЕ нужен. Ответ — bare
``SkillCommentDTO``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as output_module
from skillery_cli.commands import _common, comment as comment_mod
from skillery_cli.config import ClientConfig


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


# ==================== comment-edit ====================
def test_cmd_comment_edit_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    _cfg(monkeypatch, ["comment.edit_own"])

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _edit(comment_id: str, *, body: str) -> dict:
        captured.update({"comment_id": comment_id, "body": body})
        return {
            "id": comment_id,
            "skill_id": "5",
            "user_id": "u1",
            "parent_id": None,
            "body": body,
            "screenshots": [],
            "created_at": "2026-06-10T12:00:00Z",
            "updated_at": "2026-06-10T12:10:00Z",
            "is_deleted": False,
        }

    async def _close() -> None:
        return None

    fake_client.edit_comment = _edit
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    comment_mod.cmd_comment_edit(comment_id="99", body="corrected text")
    assert captured["comment_id"] == "99"
    assert captured["body"] == "corrected text"


def test_cmd_comment_edit_rejects_empty_body() -> None:
    _text_mode()
    import typer

    with pytest.raises(typer.Exit) as exc:
        comment_mod.cmd_comment_edit(comment_id="99", body="   ")
    assert exc.value.exit_code == 1


# ==================== comment-delete ====================
def test_cmd_comment_delete_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    _cfg(monkeypatch, ["comment.delete_own"])

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _delete(comment_id: str) -> dict:
        captured["comment_id"] = comment_id
        return {
            "id": comment_id,
            "skill_id": "5",
            "user_id": "u1",
            "parent_id": None,
            "body": "[deleted]",
            "screenshots": [],
            "created_at": "2026-06-10T12:00:00Z",
            "updated_at": "2026-06-10T12:10:00Z",
            "deleted_at": "2026-06-10T12:10:00Z",
            "is_deleted": True,
        }

    async def _close() -> None:
        return None

    fake_client.delete_comment = _delete
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    comment_mod.cmd_comment_delete(comment_id="99")
    assert captured["comment_id"] == "99"


# ==================== registration ====================
def test_comment_edit_delete_registered_with_perms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "comment.edit_own", "comment.delete_own"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "comment-edit" in names
    assert "comment-delete" in names


def test_comment_edit_gated_by_edit_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """comment-edit появляется только с comment.edit_own."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "comment-edit" not in names
    assert "comment-delete" not in names


def test_comment_delete_gated_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """edit_own без delete_own → только comment-edit."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "comment.edit_own"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "comment-edit" in names
    assert "comment-delete" not in names
