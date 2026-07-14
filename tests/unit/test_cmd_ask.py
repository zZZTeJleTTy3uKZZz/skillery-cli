"""RE2: skillery ask — реальный AI-адвайзер (SSE) из CLI."""
from __future__ import annotations

from typing import Any

import pytest
import respx
from httpx import Response

from skillery_cli.core.transport import HubClient

_SSE = (
    "event: meta\n"
    'data: {"conversation_id": 7}\n\n'
    "event: token\n"
    'data: {"text": "Привет"}\n\n'
    "event: token\n"
    'data: {"text": ", вот навык"}\n\n'
    "event: skills\n"
    'data: {"skills": [{"skill_id": 3, "slug": "atlas", "title": "Atlas", '
    '"score": 0.9, "reason": "ведёт задачи"}]}\n\n'
    "event: done\n"
    'data: {"conversation_id": 7}\n\n'
)


@pytest.mark.asyncio
async def test_advisor_stream_parses_sse_events() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        router.post("/advisor/messages").mock(
            return_value=Response(
                200, text=_SSE, headers={"content-type": "text/event-stream"}
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        events: list[tuple[str, dict]] = []
        try:
            async for ev, data in client.advisor_stream(message="помоги с задачами"):
                events.append((ev, data))
        finally:
            await client.close()

    kinds = [e for e, _ in events]
    assert kinds == ["meta", "token", "token", "skills", "done"]
    assert events[0][1]["conversation_id"] == 7
    answer = "".join(d["text"] for e, d in events if e == "token")
    assert answer == "Привет, вот навык"
    skills = next(d for e, d in events if e == "skills")["skills"]
    assert skills[0]["slug"] == "atlas"


@pytest.mark.asyncio
async def test_advisor_stream_refreshes_on_401() -> None:
    """401 → on_refresh → повтор стрима с новым токеном (без двойного закрытия)."""
    calls = {"n": 0}

    def _responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(401, json={"detail": "expired"})
        return Response(
            200, text=_SSE, headers={"content-type": "text/event-stream"}
        )

    async def _refresh():
        return ("new-access", "new-refresh")

    with respx.mock(base_url="http://localhost:8000") as router:
        router.post("/advisor/messages").mock(side_effect=_responder)
        client = HubClient(
            base_url="http://localhost:8000", access_token="old",
            on_token_refresh=_refresh,
        )
        kinds: list[str] = []
        try:
            async for ev, _ in client.advisor_stream(message="q"):
                kinds.append(ev)
        finally:
            await client.close()

    assert calls["n"] == 2  # был повтор после refresh
    assert kinds == ["meta", "token", "token", "skills", "done"]


@pytest.mark.asyncio
async def test_advisor_stream_network_error_is_apierror() -> None:
    """Сетевой сбой стрима → чистая ApiError(NETWORK), не сырой httpx-traceback."""
    import httpx

    from skillery_cli.core.transport import ApiError

    with respx.mock(base_url="http://localhost:8000") as router:
        router.post("/advisor/messages").mock(
            side_effect=httpx.ConnectError("boom")
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            with pytest.raises(ApiError) as ei:
                async for _ in client.advisor_stream(message="q"):
                    pass
        finally:
            await client.close()
    assert ei.value.code == "NETWORK"


def test_cmd_ask_collects_answer_and_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_ask агрегирует стрим в payload {conversation_id, answer, skills}."""
    from skillery_cli.commands import ask as ask_mod
    from skillery_cli.config import ClientConfig

    cfg = ClientConfig(base_url="http://x", user_email="u@x.io")
    cfg.permissions = ["skill.read"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ask_mod, "load_tokens", lambda email: ("acc", "ref"), raising=False)
    monkeypatch.setattr(
        "skillery_cli.config.load_tokens", lambda email: ("acc", "ref")
    )
    monkeypatch.setattr(ask_mod, "is_json", lambda: True)

    class _FakeClient:
        async def advisor_stream(self, *, message: str, conversation_id: int | None = None):
            yield "meta", {"conversation_id": 42}
            yield "token", {"text": "Ответ "}
            yield "token", {"text": "модели"}
            yield "skills", {"skills": [{"slug": "atlas", "title": "Atlas", "reason": "r"}]}
            yield "done", {"conversation_id": 42}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(ask_mod, "make_client", lambda cfg, access: _FakeClient())

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        ask_mod, "emit_data", lambda data, **kw: captured.update({"p": data})
    )

    ask_mod.cmd_ask(query="помоги", conversation_id=None)

    p = captured["p"]
    assert p["conversation_id"] == 42
    assert p["answer"] == "Ответ модели"
    assert p["skills"][0]["slug"] == "atlas"
