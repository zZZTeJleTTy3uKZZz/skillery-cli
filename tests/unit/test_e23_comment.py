"""Тесты ``skillery comment`` + ``skillery comments`` (E23)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response

from skillery_cli import output as output_module
from skillery_cli.commands import _common, comment as comment_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import HubClient


def _text_mode() -> None:
    output_module._mode = "text"


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("access", "refresh"))


# ---------------------- HubClient ----------------------
@pytest.mark.asyncio
async def test_hubclient_post_comment_json() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/skills/slk_x/comments").mock(
            return_value=Response(
                201,
                json={
                    "comment": {
                        "id": "cmt_1",
                        "skill_id": "slk_x",
                        "user_id": "u1",
                        "parent_id": None,
                        "body": "hello",
                        "screenshots": [],
                        "created_at": "2026-05-26T12:00:00Z",
                        "is_deleted": False,
                    }
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.post_comment("slk_x", body="hello")
        finally:
            await client.close()
        assert route.called
        assert r["comment"]["body"] == "hello"


@pytest.mark.asyncio
async def test_hubclient_post_comment_multipart_sends_files() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/skills/slk_x/comments/multipart").mock(
            return_value=Response(
                201,
                json={
                    "comment": {
                        "id": "cmt_2",
                        "skill_id": "slk_x",
                        "user_id": "u1",
                        "parent_id": None,
                        "body": "screenshot here",
                        "screenshots": ["/uploads/comments/cmt_2/0_a.png"],
                        "created_at": "2026-05-26T12:00:00Z",
                        "is_deleted": False,
                    }
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.post_comment_multipart(
                "slk_x",
                body="screenshot here",
                screenshots=[("a.png", b"\x89PNG\r\nfake")],
            )
        finally:
            await client.close()
        assert route.called
        req_body = route.calls[0].request.read()
        assert b"a.png" in req_body
        assert r["comment"]["screenshots"]


@pytest.mark.asyncio
async def test_hubclient_list_comments_passes_cursor() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills/slk_x/comments").mock(
            return_value=Response(
                200,
                json={"data": [], "has_more": False, "next_cursor": None, "object": "list"},
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.list_comments("slk_x", limit=25, starting_after="cmt_abc")
        finally:
            await client.close()
        assert route.called
        params = dict(route.calls[0].request.url.params)
        assert params.get("limit") == "25"
        assert params.get("starting_after") == "cmt_abc"


# ---------------------- cmd_comment_post ----------------------
def test_cmd_comment_post_rejects_empty_body() -> None:
    _text_mode()
    import typer

    with pytest.raises(typer.Exit) as exc:
        comment_mod.cmd_comment_post(slug="my-skill", body="   ", parent_id=None, screenshot=None)
    assert exc.value.exit_code == 1


def test_cmd_comment_post_json_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "comment.post"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _resolve(client, slug):  # noqa: ANN001
        return "slk_resolved"

    monkeypatch.setattr(_common, "resolve_skill_id", _resolve)

    async def _post(skill_id: str, *, body: str, parent_id: str | None = None) -> dict:
        captured["skill_id"] = skill_id
        captured["body"] = body
        captured["parent"] = parent_id
        return {
            "comment": {
                "id": "cmt_x",
                "skill_id": skill_id,
                "user_id": "u1",
                "body": body,
                "screenshots": [],
                "created_at": "2026-05-26T12:00:00Z",
                "is_deleted": False,
            }
        }

    async def _post_multipart(*args, **kw):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("multipart не должен звониться без скриншотов")

    async def _close() -> None:
        return None

    fake_client.post_comment = _post
    fake_client.post_comment_multipart = _post_multipart
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    comment_mod.cmd_comment_post(
        slug="my-skill", body="nice skill", parent_id=None, screenshot=None
    )
    assert captured["body"] == "nice skill"
    assert captured["skill_id"] == "slk_resolved"


def test_cmd_comment_post_multipart_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "comment.post"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"\x89PNG\r\nbinary")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _resolve(client, slug):  # noqa: ANN001
        return "slk_resolved"

    monkeypatch.setattr(_common, "resolve_skill_id", _resolve)

    async def _post(*args, **kw):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("simple POST не должен звониться")

    async def _post_multipart(
        skill_id: str,
        *,
        body: str,
        screenshots,  # type: ignore[no-untyped-def]
        parent_id: str | None = None,
    ) -> dict:
        captured["files"] = [n for n, _ in screenshots]
        return {
            "comment": {
                "id": "cmt_mp",
                "skill_id": skill_id,
                "user_id": "u1",
                "body": body,
                "screenshots": ["/uploads/cmt_mp/0_shot.png"],
                "created_at": "2026-05-26T12:00:00Z",
                "is_deleted": False,
            }
        }

    async def _close() -> None:
        return None

    fake_client.post_comment = _post
    fake_client.post_comment_multipart = _post_multipart
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    comment_mod.cmd_comment_post(
        slug="my-skill",
        body="bug",
        parent_id=None,
        screenshot=[screenshot],
    )
    assert captured["files"] == ["shot.png"]


def test_cmd_comment_post_fails_when_screenshot_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    import typer

    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "comment.post"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))

    missing = tmp_path / "nope.png"
    with pytest.raises(typer.Exit) as exc:
        comment_mod.cmd_comment_post(
            slug="my-skill",
            body="hi",
            parent_id=None,
            screenshot=[missing],
        )
    assert exc.value.exit_code == 1


# ---------------------- cmd_comments_list ----------------------
def test_cmd_comments_list_calls_list(monkeypatch: pytest.MonkeyPatch) -> None:
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
        return "slk_x"

    monkeypatch.setattr(_common, "resolve_skill_id", _resolve)

    async def _list(
        skill_id: str,
        *,
        limit: int = 50,
        starting_after: str | None = None,
    ) -> dict[str, Any]:
        captured["skill_id"] = skill_id
        captured["limit"] = limit
        captured["cursor"] = starting_after
        return {
            "data": [
                {
                    "id": "cmt_1",
                    "user_id": "u_one",
                    "body": "hi",
                    "created_at": "2026-05-26T12:00:00Z",
                }
            ],
            "has_more": False,
            "next_cursor": None,
        }

    async def _close() -> None:
        return None

    fake_client.list_comments = _list
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    comment_mod.cmd_comments_list(slug="my-skill", limit=25, starting_after="cmt_prev")
    assert captured["limit"] == 25
    assert captured["cursor"] == "cmt_prev"


def test_comments_registered_with_skill_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`comments` (list) — нужен только skill.read."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "comments" in names
    assert "comment" not in names  # post требует comment.post
