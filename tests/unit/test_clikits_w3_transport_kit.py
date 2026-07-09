"""cli-kits W3: HubClient на librarykit.transport как сетевой слой.

Покрывает перевод низкоуровневого HTTP HubClient на
``librarykit.transport.HttpxTransport`` (вместо прямого ``httpx.AsyncClient``),
СОХРАНИВ публичный контракт:

- сетевым choke-point'ом владеет librarykit-транспорт (HubClient не держит
  ``httpx.AsyncClient`` напрямую);
- W1 method-aware retry цел: idempotent GET ретраится, мутации — нет;
- сетевой сбой при исчерпании бюджета → доменный ``librarykit.errors.
  TransportError`` (транспорт кита оборачивает httpx-ошибку);
- 401-auto-refresh через tuple-callback (``() -> (access, refresh)``) сохранён —
  CLI-контракт refresh-колбэка не сломан;
- User-Agent CLI уходит на каждый запрос (backend различает CLI/WEB-сессии);
- multipart по-прежнему ходит через тот же транспорт (data + files).
"""
from __future__ import annotations

import httpx
import librarykit.errors as lk_errors
import pytest
import respx
from httpx import Response
from librarykit.transport import HttpxTransport

from skillery_cli.core.transport import USER_AGENT, ApiError, HubClient


def test_hubclient_network_layer_is_librarykit_transport() -> None:
    """Сетевой слой HubClient — librarykit ``HttpxTransport`` (не голый httpx)."""
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    assert isinstance(client._transport, HttpxTransport)


@pytest.mark.asyncio
async def test_get_retries_idempotent_via_kit_transport() -> None:
    """GET идемпотентен → 503 повторяем, затем 200 (W1 method-aware retry цел)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills")
        route.side_effect = [
            Response(503, text="unavailable"),
            Response(200, json=[{"slug": "x"}]),
        ]
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            data = await client.list_skills()
        finally:
            await client.close()
    assert data == [{"slug": "x"}]
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_post_not_retried_on_5xx_via_kit_transport() -> None:
    """POST не идемпотентен → даже на 5xx НЕ повторяем (риск двойной мутации)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/skills")
        route.mock(return_value=Response(503, text="unavailable"))
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            with pytest.raises(ApiError) as exc:
                await client.publish_skill({"slug": "x"})
        finally:
            await client.close()
    assert exc.value.status_code == 503
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_network_error_exhaust_raises_librarykit_transport_error() -> None:
    """Сетевая ошибка на каждой попытке GET → librarykit.TransportError.

    Транспорт кита оборачивает ``httpx.TransportError`` в доменный
    ``librarykit.errors.TransportError`` (часть единой иерархии ошибок китов).
    """
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills")
        route.side_effect = httpx.ConnectError("down")
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            with pytest.raises(lk_errors.TransportError):
                await client.list_skills()
        finally:
            await client.close()
    assert route.call_count >= 2


@pytest.mark.asyncio
async def test_user_agent_sent_on_every_request() -> None:
    """CLI User-Agent уходит на каждый запрос через транспорт кита."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills").mock(return_value=Response(200, json=[]))
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.list_skills()
        finally:
            await client.close()
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT


@pytest.mark.asyncio
async def test_401_auto_refresh_tuple_callback_preserved() -> None:
    """401 → tuple-refresh-callback → повтор с новым токеном (CLI-контракт цел)."""
    calls: list[str] = []

    async def _refresh() -> tuple[str, str] | None:
        calls.append("refreshed")
        return ("new-access", "new-refresh")

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/me/permissions")
        route.side_effect = [
            Response(401, json={"detail": "token invalid"}),
            Response(200, json={"permissions": ["skill.read"]}),
        ]
        client = HubClient(
            base_url="http://localhost:8000",
            access_token="old",
            on_token_refresh=_refresh,
        )
        try:
            perms = await client.get_me_permissions()
        finally:
            await client.close()
    assert perms == ["skill.read"]
    assert calls == ["refreshed"]
    # повтор ушёл уже с обновлённым Bearer
    assert route.calls.last.request.headers["Authorization"] == "Bearer new-access"


@pytest.mark.asyncio
async def test_multipart_still_routes_through_transport() -> None:
    """multipart-комментарий ходит через тот же транспорт (data + files)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/skills/5/comments/multipart").mock(
            return_value=Response(201, json={"comment": {"id": "c1"}})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.post_comment_multipart(
                "5", body="hi", screenshots=[("a.png", b"\x89PNG")]
            )
        finally:
            await client.close()
    assert route.called
    req_body = route.calls.last.request.read()
    assert b"a.png" in req_body
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT
    assert r["comment"]["id"] == "c1"
