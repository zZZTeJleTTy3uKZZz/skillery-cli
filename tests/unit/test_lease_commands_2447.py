"""#2447: ``skillery lease list/get`` — журнал действующих лизов.

До этой группы роуты ``GET /me/leases`` и ``GET /me/leases/{id}`` не звал
никто: CLI умел лизы только выписывать, и любая проблема с правом выглядела
одинаково — «нет доступа». Тесты держат ровно то, ради чего группа заведена:

а) команды реально ходят в эти два пути (сеть — через respx, а не фейк:
   фейковый клиент не ловит опечатку в URL);
б) сужение до одного устройства уходит параметром, а ``--this-device``
   подставляет id ЭТОЙ машины — иначе журнал парка не отвечает на вопрос
   «почему у меня тут не работает»;
в) человек видит «до когда», а не только дату истечения;
г) группа зарегистрирована и доступна БЕЗ особых прав.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
import respx
from httpx import Response

from skillery_cli import output as output_module
from skillery_cli.commands import _common
from skillery_cli.commands import lease as lease_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import HubClient

_BASE = "http://localhost:8000"

JTI = "9f1c2d3e-0000-4000-8000-000000000001"


def _lease(**over: Any) -> dict[str, Any]:
    row = {
        "jti": JTI,
        "capability": "grok_transcriber",
        "capability_id": 318,
        "skill_id": 42,
        "device_id": "dev-1",
        "issued_at": "2026-08-30T10:00:00Z",
        "expires_at": "2026-08-31T10:00:00Z",
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def text_mode() -> None:
    output_module._mode = "text"


# ============================================================
# (а)+(б) транспорт: путь, метод, сужение по устройству
# ============================================================
@pytest.mark.asyncio
@respx.mock
async def test_list_my_leases_hits_me_leases() -> None:
    route = respx.get(f"{_BASE}/me/leases").mock(
        return_value=Response(200, json={"items": [_lease()], "total": 1})
    )
    client = HubClient(base_url=_BASE, access_token="tok")
    try:
        data = await client.list_my_leases()
    finally:
        await client.close()

    assert route.called
    assert "device_id" not in route.calls[0].request.url.params
    assert data["total"] == 1
    assert data["items"][0]["jti"] == JTI


@pytest.mark.asyncio
@respx.mock
async def test_list_my_leases_narrows_to_device() -> None:
    """Без сужения журнал парка из десятка машин не отвечает на вопрос
    «почему не работает ЗДЕСЬ»."""
    route = respx.get(f"{_BASE}/me/leases").mock(
        return_value=Response(200, json={"items": [], "total": 0})
    )
    client = HubClient(base_url=_BASE, access_token="tok")
    try:
        await client.list_my_leases(device_id="dev-7")
    finally:
        await client.close()

    assert route.calls[0].request.url.params["device_id"] == "dev-7"


@pytest.mark.asyncio
@respx.mock
async def test_get_my_lease_hits_item_route() -> None:
    route = respx.get(f"{_BASE}/me/leases/{JTI}").mock(
        return_value=Response(200, json=_lease())
    )
    client = HubClient(base_url=_BASE, access_token="tok")
    try:
        item = await client.get_my_lease(JTI)
    finally:
        await client.close()

    assert route.called
    assert item["capability"] == "grok_transcriber"


# ============================================================
# (в) «до когда» — главный ответ группы
# ============================================================
class TestRemaining:
    def test_hours_and_minutes_are_human(self) -> None:
        now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
        assert lease_mod.remaining("2026-08-30T13:30:00Z", now=now) == "3 ч 30 мин"

    def test_days_are_days(self) -> None:
        now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
        assert lease_mod.remaining("2026-09-01T12:00:00Z", now=now) == "2 дн 2 ч"

    def test_past_is_expired_not_negative(self) -> None:
        """Часы машины могут убежать вперёд — отрицательный срок врал бы."""
        now = datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)
        assert lease_mod.remaining("2026-08-30T10:00:00Z", now=now) == "истёк"

    def test_garbage_does_not_crash(self) -> None:
        assert lease_mod.remaining("не-дата") == "—"
        assert lease_mod.remaining(None) == "—"


# ============================================================
# команды: сужение и вывод
# ============================================================
class _Client:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.device_id: Any = "не-звали"

    async def list_my_leases(self, *, device_id: str | None = None):  # type: ignore[no-untyped-def]
        self.device_id = device_id
        return self._data

    async def close(self) -> None:
        return None


def _wire(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    monkeypatch.setattr(_common, "make_client", lambda *a, **kw: client)
    monkeypatch.setattr(_common, "get_access_token", lambda: "tok")
    monkeypatch.setattr(
        ClientConfig, "load", classmethod(lambda cls: ClientConfig(base_url=_BASE))
    )


class TestListCommand:
    def test_this_device_substitutes_local_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Свой client_device_id человек наизусть не помнит."""
        client = _Client({"items": [], "total": 0})
        _wire(monkeypatch, client)
        monkeypatch.setattr(
            "skillery_cli.core.identity.device_uid", lambda: "dev-local"
        )
        lease_mod.cmd_lease_list(device=None, this_device=True)
        assert client.device_id == "dev-local"

    def test_json_payload_carries_items(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = _Client({"items": [_lease()], "total": 1})
        _wire(monkeypatch, client)
        output_module._mode = "json"
        try:
            lease_mod.cmd_lease_list(device="dev-1", this_device=False)
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        finally:
            output_module._mode = "text"
        assert client.device_id == "dev-1"
        assert payload["items"][0]["jti"] == JTI


# ============================================================
# (г) регистрация группы — без гейта прав
# ============================================================
def test_group_is_available_without_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Прав никаких, кроме базового чтения, — группа всё равно есть."""
    cfg = ClientConfig(
        base_url=_BASE, user_email="u@example.com", permissions=["skill.read"]
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    group = next(g for g in app.registered_groups if g.name == "lease")
    names = {c.name for c in group.typer_instance.registered_commands}
    assert {"list", "get"} <= names
