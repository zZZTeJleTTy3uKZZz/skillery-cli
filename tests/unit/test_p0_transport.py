"""Тесты НОВЫХ transport-методов (P0-команды).

Сверены с реальными backend-роутами:
- ``POST /support/tickets/{id}/messages`` (JSON) + ``/multipart`` —
  ``backend/.../routes/support_tickets.py``.
- ``PATCH /support/tickets/{id}`` — там же. Статусы по домену
  ``TicketStatus`` = new|in_progress|scheduled|done|rejected.
- ``PATCH /comments/{id}`` + ``DELETE /comments/{id}`` —
  ``backend/.../routes/skill_review.py``. Bare ``SkillCommentDTO`` (НЕ wrapped).
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from skillery_cli.core.transport import HubClient


# ---------------------- ticket reply (JSON) ----------------------
@pytest.mark.asyncio
async def test_reply_ticket_json_posts_messages_endpoint() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/support/tickets/42/messages").mock(
            return_value=Response(
                201,
                json={
                    "message": {
                        "id": "msg_1",
                        "ticket_id": "42",
                        "author_id": "u1",
                        "body": "any update?",
                        "parent_id": None,
                        "screenshots": [],
                        "created_at": "2026-06-10T12:00:00Z",
                        "is_deleted": False,
                    }
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.reply_ticket("42", body="any update?")
        finally:
            await client.close()
        assert route.called
        body = route.calls[0].request.read()
        assert b"any update?" in body
        assert r["message"]["id"] == "msg_1"


@pytest.mark.asyncio
async def test_reply_ticket_json_passes_parent_id() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/support/tickets/7/messages").mock(
            return_value=Response(
                201,
                json={
                    "message": {
                        "id": "msg_2",
                        "ticket_id": "7",
                        "author_id": "u1",
                        "body": "re",
                        "parent_id": "msg_1",
                        "screenshots": [],
                        "created_at": "2026-06-10T12:00:00Z",
                        "is_deleted": False,
                    }
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.reply_ticket("7", body="re", parent_id="msg_1")
        finally:
            await client.close()
        body = route.calls[0].request.read()
        assert b"msg_1" in body


# ---------------------- ticket reply (multipart) ----------------------
@pytest.mark.asyncio
async def test_reply_ticket_multipart_sends_files() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/support/tickets/42/messages").mock(
            return_value=Response(
                201,
                json={
                    "message": {
                        "id": "msg_mp",
                        "ticket_id": "42",
                        "author_id": "u1",
                        "body": "see screenshot",
                        "parent_id": None,
                        "screenshots": ["/uploads/tickets/42/0_a.png"],
                        "created_at": "2026-06-10T12:00:00Z",
                        "is_deleted": False,
                    }
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.reply_ticket_multipart(
                "42",
                body="see screenshot",
                screenshots=[("a.png", b"\x89PNG\r\nfake")],
            )
        finally:
            await client.close()
        assert route.called
        req_body = route.calls[0].request.read()
        assert b"a.png" in req_body
        assert r["message"]["screenshots"]


# ---------------------- ticket status (PATCH) ----------------------
@pytest.mark.asyncio
async def test_set_ticket_status_patches_ticket() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.patch("/support/tickets/42").mock(
            return_value=Response(
                200,
                json={
                    "id": "42",
                    "company_id": "1",
                    "creator_id": "u1",
                    "assignee_id": None,
                    "skill_id": None,
                    "kind": "bug",
                    "priority": "normal",
                    "status": "in_progress",
                    "subject": "S",
                    "body": "B",
                    "screenshots": [],
                    "created_at": "2026-06-10T12:00:00Z",
                    "updated_at": "2026-06-10T12:05:00Z",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.set_ticket_status("42", status="in_progress")
        finally:
            await client.close()
        assert route.called
        assert route.calls[0].request.method == "PATCH"
        body = route.calls[0].request.read()
        assert b"in_progress" in body
        assert r["status"] == "in_progress"


# ---------------------- comment edit (PATCH /comments/{id}) ----------------------
@pytest.mark.asyncio
async def test_edit_comment_patches_bare_dto() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.patch("/comments/99").mock(
            return_value=Response(
                200,
                json={
                    "id": "99",
                    "skill_id": "5",
                    "user_id": "u1",
                    "parent_id": None,
                    "body": "edited body",
                    "screenshots": [],
                    "created_at": "2026-06-10T12:00:00Z",
                    "updated_at": "2026-06-10T12:10:00Z",
                    "is_deleted": False,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.edit_comment("99", body="edited body")
        finally:
            await client.close()
        assert route.called
        body = route.calls[0].request.read()
        assert b"edited body" in body
        # bare DTO (НЕ wrapped в {"comment": ...})
        assert r["body"] == "edited body"
        assert r["id"] == "99"


# ---------------------- comment delete (DELETE /comments/{id}) ----------------------
@pytest.mark.asyncio
async def test_delete_comment_soft_deletes() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.delete("/comments/99").mock(
            return_value=Response(
                200,
                json={
                    "id": "99",
                    "skill_id": "5",
                    "user_id": "u1",
                    "parent_id": None,
                    "body": "[deleted]",
                    "screenshots": [],
                    "created_at": "2026-06-10T12:00:00Z",
                    "updated_at": "2026-06-10T12:10:00Z",
                    "deleted_at": "2026-06-10T12:10:00Z",
                    "is_deleted": True,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.delete_comment("99")
        finally:
            await client.close()
        assert route.called
        assert route.calls[0].request.method == "DELETE"
        assert r["is_deleted"] is True
