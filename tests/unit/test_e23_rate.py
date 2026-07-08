"""Тесты ``skillery rate`` + ``rating-summary`` (E23)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response

from skillery_cli import output as output_module
from skillery_cli.commands import _common, rate as rate_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import HubClient


def _text_mode() -> None:
    output_module._mode = "text"


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    """Force рейс через `_common.HubClient` mock."""
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("access", "refresh"))


# ---------------------- HubClient methods ----------------------
@pytest.mark.asyncio
async def test_hubclient_rate_skill_posts_score() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/skills/slk_x/ratings").mock(
            return_value=Response(
                200,
                json={
                    "id": "rat_1",
                    "skill_id": "slk_x",
                    "user_id": "u1",
                    "score": 4,
                    "created_at": "2026-05-26T12:00:00Z",
                    "updated_at": None,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.rate_skill("slk_x", 4)
        finally:
            await client.close()
        assert route.called
        assert r["score"] == 4
        body = route.calls[0].request.read()
        assert b'"score": 4' in body or b'"score":4' in body


@pytest.mark.asyncio
async def test_hubclient_rating_summary() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/skills/slk_x/ratings/summary").mock(
            return_value=Response(
                200,
                json={
                    "skill_id": "slk_x",
                    "avg": 4.25,
                    "count": 8,
                    "distribution": [0, 0, 1, 4, 3],
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            s = await client.get_rating_summary("slk_x")
        finally:
            await client.close()
        assert s["avg"] == 4.25
        assert s["count"] == 8


# ---------------------- cmd_rate ----------------------
def test_cmd_rate_validates_score_range() -> None:
    _text_mode()
    import typer

    with pytest.raises(typer.Exit) as exc:
        rate_mod.cmd_rate(slug="my-skill", score=7)
    assert exc.value.exit_code == 1


def test_cmd_rate_calls_rate_then_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "skill.rate"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _resolve_skill_id(client, slug_or_id):  # noqa: ANN001
        return "slk_resolved"

    monkeypatch.setattr(_common, "resolve_skill_id", _resolve_skill_id)

    async def _fake_rate(skill_id: str, score: int) -> dict[str, Any]:
        captured["skill_id"] = skill_id
        captured["score"] = score
        return {"id": "rat_1", "skill_id": skill_id, "score": score, "user_id": "u1"}

    async def _fake_summary(skill_id: str) -> dict[str, Any]:
        return {
            "skill_id": skill_id,
            "avg": 4.5,
            "count": 2,
            "distribution": [0, 0, 0, 1, 1],
        }

    async def _close() -> None:
        return None

    fake_client.rate_skill = _fake_rate
    fake_client.get_rating_summary = _fake_summary
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    rate_mod.cmd_rate(slug="my-skill", score=5)
    assert captured["score"] == 5
    assert captured["skill_id"] == "slk_resolved"


def test_cmd_rating_summary_just_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()
    called: dict[str, Any] = {}

    async def _resolve(client, slug):  # noqa: ANN001
        return slug if slug.startswith("slk_") else "slk_resolved"

    monkeypatch.setattr(_common, "resolve_skill_id", _resolve)

    async def _summary(skill_id: str) -> dict[str, Any]:
        called["skill_id"] = skill_id
        return {
            "skill_id": skill_id,
            "avg": 3.7,
            "count": 10,
            "distribution": [1, 1, 2, 4, 2],
        }

    async def _close() -> None:
        return None

    fake_client.get_rating_summary = _summary
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    rate_mod.cmd_rating_summary(slug="slk_direct")
    assert called["skill_id"] == "slk_direct"


def test_cmd_rate_registered_when_has_skill_rate_perm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "skill.rate"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "rate" in names
    assert "rating-summary" in names


def test_cmd_rate_not_registered_without_perm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],  # без skill.rate
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "rate" not in names
