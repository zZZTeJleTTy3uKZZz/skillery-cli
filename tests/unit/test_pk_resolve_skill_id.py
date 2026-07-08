"""PK-миграция (§3.E): дискриминатор ``resolve_skill_id`` (id-или-slug).

Правила:
- значение из одних цифр → это id, отдаём как есть (без сетевого запроса);
- иначе → slug, резолвим через ``GET /skills/{slug}`` и берём числовой id;
- 404 при несуществующем slug пробрасывается как ``ApiError``.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from skillery_cli.commands import _common
from skillery_cli.core.transport import ApiError, HubClient


@pytest.mark.asyncio
async def test_numeric_value_is_passed_through_without_request() -> None:
    """Числовой id отдаётся как есть и НЕ ходит на backend."""
    # assert_all_called=False: route намеренно НЕ вызывается (fast-path для id).
    # Новые версии respx по умолчанию ассертят вызов всех routes.
    with respx.mock(base_url="http://localhost:8000", assert_all_called=False) as router:
        route = router.get("/skills/123").mock(return_value=Response(200, json={"id": 999}))
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            resolved = await _common.resolve_skill_id(client, "123")
        finally:
            await client.close()
        assert resolved == "123"
        # Fast-path: запроса быть не должно.
        assert not route.called


@pytest.mark.asyncio
async def test_slug_is_resolved_to_numeric_id() -> None:
    """Не-числовой slug резолвится в строковый числовой id из ответа backend."""
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/skills/bitrix24").mock(
            return_value=Response(200, json={"id": 42, "slug": "bitrix24"})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            resolved = await _common.resolve_skill_id(client, "bitrix24")
        finally:
            await client.close()
        assert resolved == "42"


@pytest.mark.asyncio
async def test_int_id_in_response_is_stringified() -> None:
    """Даже если backend вернул id как JSON-int — наружу уходит строка."""
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/skills/demo-test").mock(
            return_value=Response(200, json={"id": 7})
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resolved = await _common.resolve_skill_id(client, "demo-test")
        finally:
            await client.close()
        assert resolved == "7"
        assert isinstance(resolved, str)


@pytest.mark.asyncio
async def test_unknown_slug_404_propagates_as_apierror() -> None:
    """404 на неизвестный slug → ApiError (команда переведёт в exit(1))."""
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/skills/nope").mock(
            return_value=Response(
                404,
                json={"error": {"code": "NOT_FOUND", "message": "no such skill"}},
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            with pytest.raises(ApiError) as exc:
                await _common.resolve_skill_id(client, "nope")
        finally:
            await client.close()
        assert exc.value.status_code == 404
