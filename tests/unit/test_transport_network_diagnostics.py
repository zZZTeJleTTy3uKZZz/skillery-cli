"""Внятная диагностика сетевого сбоя (инцидент 2026-07-24).

В ``~/.skillery/last-error.log`` лежало:

    librarykit.errors.TransportError: ConnectError:

Текст httpx-исключения ПУСТОЙ, хоста нет, тип причины не виден — по такому
сообщению нельзя понять ни куда не достучались, ни что именно сломалось.
Теперь сетевая ошибка несёт ``METHOD scheme://host/path`` (БЕЗ query — там
бывают токены), тип исключения и причину.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from librarykit.errors import TransportError

from skillery_cli.core.transport import HubClient

BASE = "http://hub.example.test"


@pytest.fixture
async def client():
    c = HubClient(base_url=BASE, access_token="tok")
    try:
        yield c
    finally:
        await c.close()


def test_describe_target_keeps_host_and_path_but_drops_query(client) -> None:
    described = client._describe_target("get", "/me/device-queue?wait=25&token=SECRET")
    assert described == f"GET {BASE}/me/device-queue"
    assert "SECRET" not in described
    assert "wait=" not in described


def test_network_error_adds_type_when_text_is_empty(client) -> None:
    err = client._network_error("GET", "/me/device-queue", httpx.ConnectError(""))
    text = str(err)
    assert "hub.example.test" in text
    assert "/me/device-queue" in text
    assert "ConnectError" in text, "тип исключения потерян — сообщение бесполезно"


def test_network_error_keeps_cause(client) -> None:
    inner = OSError("не удалось установить соединение")
    outer = httpx.ConnectError("")
    outer.__cause__ = inner
    err = client._network_error("GET", "/skills", outer)
    assert "OSError" in str(err)
    assert "соединение" in str(err)


@respx.mock
async def test_mutation_surfaces_enriched_transport_error() -> None:
    """POST не ретраится → наружу летит ОБОГАЩЁННАЯ доменная ошибка."""
    respx.post(f"{BASE}/events").mock(side_effect=httpx.ConnectError(""))
    c = HubClient(base_url=BASE, access_token="tok")
    try:
        with pytest.raises(TransportError) as exc:
            await c._request("POST", "/events", json={})
    finally:
        await c.close()
    text = str(exc.value)
    assert "POST" in text
    assert "hub.example.test" in text
    assert "/events" in text
    assert "ConnectError" in text
