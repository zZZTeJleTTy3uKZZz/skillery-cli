"""cli-kits W1: интеграция librarykit.errors + retry в HubClient-транспорт.

Покрывает:
- ApiError остаётся обратносовместимым (status_code/code/message/details,
  __str__, kwargs-конструктор) И попадает в иерархию librarykit (CliError);
- idempotent GET ретраится на 5xx / сетевых ошибках / 429;
- НЕ-идемпотентные мутации (POST/PATCH/PUT/DELETE) НЕ ретраятся;
- 4xx (кроме 429) НЕ ретраятся;
- 429 уважает Retry-After;
- сетевые ошибки httpx маппятся в доменный TransportError при исчерпании
  бюджета ретраев.
"""
from __future__ import annotations

import httpx
import librarykit.errors as lk_errors
import pytest
import respx
from httpx import Response

from skillery_cli.core.transport import ApiError, HubClient


# --------------------- ApiError обратная совместимость + иерархия --------------
def test_apierror_is_librarykit_clierror_subclass() -> None:
    """ApiError должен попадать в иерархию librarykit (ловится как CliError/
    librarykit.ApiError), чтобы общий обработчик китов его видел."""
    err = ApiError(
        status_code=403, code="PERMISSION_DENIED", message="нет прав", details={}
    )
    assert isinstance(err, lk_errors.CliError)
    assert isinstance(err, lk_errors.ApiError)  # ApiError is CliError в ките


def test_apierror_keeps_public_contract() -> None:
    """Публичный контракт CLI не должен сломаться: kwargs-конструктор,
    атрибуты, __str__."""
    err = ApiError(
        status_code=409,
        code="EMAIL_TAKEN",
        message="занят",
        details={"field": "email"},
    )
    assert err.status_code == 409
    assert err.code == "EMAIL_TAKEN"
    assert err.message == "занят"
    assert err.details == {"field": "email"}
    assert str(err) == "[409/EMAIL_TAKEN] занят"


# ------------------------------ retry: idempotent GET -------------------------
@pytest.mark.asyncio
async def test_get_retries_on_5xx_then_succeeds() -> None:
    """GET идемпотентен → на 503 повторяем, затем 200."""
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
async def test_get_retries_on_network_error_then_succeeds() -> None:
    """Сетевая ошибка httpx на GET → повтор, затем успех."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills")
        route.side_effect = [
            httpx.ConnectError("boom"),
            Response(200, json=[]),
        ]
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            data = await client.list_skills()
        finally:
            await client.close()
    assert data == []
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_get_retries_on_429_respecting_retry_after() -> None:
    """429 с Retry-After на GET → повтор (после паузы), затем 200."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills")
        route.side_effect = [
            Response(429, headers={"Retry-After": "0"}, text="slow down"),
            Response(200, json=[]),
        ]
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            data = await client.list_skills()
        finally:
            await client.close()
    assert data == []
    assert route.call_count == 2


# ----------------------------- НЕ ретраим то, что нельзя ----------------------
@pytest.mark.asyncio
async def test_get_does_not_retry_on_4xx() -> None:
    """4xx (кроме 429) на GET — клиентская ошибка, повтор не имеет смысла."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/skills/missing")
        route.mock(return_value=Response(404, json={"detail": "нет"}))
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            with pytest.raises(ApiError) as exc:
                await client.get_skill("missing")
        finally:
            await client.close()
    assert exc.value.status_code == 404
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_post_does_not_retry_on_5xx() -> None:
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
async def test_delete_does_not_retry_on_network_error() -> None:
    """DELETE не идемпотентен в нашем контракте → сетевая ошибка не повторяется.

    cli-kits W3: сетевой слой переехал на ``librarykit.transport.HttpxTransport``,
    который оборачивает ``httpx.TransportError`` в доменный
    ``librarykit.errors.TransportError`` (единая иерархия ошибок китов). Тип на
    выходе теперь доменный — потребители (команды) на httpx-тип не завязаны.
    """
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.delete("/comments/5")
        route.side_effect = httpx.ConnectError("down")
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            with pytest.raises(lk_errors.TransportError):
                await client.delete_comment("5")
        finally:
            await client.close()
    assert route.call_count == 1


# ------------------------- исчерпание бюджета на GG ---------------------------
@pytest.mark.asyncio
async def test_get_network_error_exhausts_budget_raises_transport_error() -> None:
    """Сетевая ошибка на КАЖДОЙ попытке GET → после бюджета пробрасываем
    доменный TransportError, бюджет конечен (не бесконечный цикл).

    cli-kits W3: транспорт кита оборачивает ``httpx.TransportError`` в
    ``librarykit.errors.TransportError`` — его и пробрасываем по исчерпании
    method-aware retry-бюджета.
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
    # бюджет SimpleRetryPolicy по умолчанию = первичная + повторы (>1, конечно)
    assert route.call_count >= 2
