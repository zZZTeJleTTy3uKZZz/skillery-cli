"""#1191: SSE-канал очереди устройства + прозрачный fallback на long-poll.

Что закрыто тестами:

а) событие ``queue`` из SSE приводит к ТЕМ ЖЕ действиям, что и long-poll
   (установка навыка + рапорт + применение ``device_tasks``);
б) разрыв → реконнект несёт ``Last-Event-ID`` с последним ОБРАБОТАННЫМ курсором
   (в т.ч. после «рестарта демона» — курсор читается с диска);
в) SSE недоступен (404 старого backend'а / сетевой обрыв) → тот же такт уходит
   на long-poll, демон жив, следующий такт тоже доставляет;
г) ``ping`` — heartbeat: не задача, курсор не двигает, очередь не применяет.

HTTP мокаем через ``respx`` (как в ``tests/unit/test_device_tasks_transport.py``):
фейковый клиент не ловит ошибки транспорта — на этом уже терялись баги.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from skillery_cli.core.transport import HubClient
from skillery_cli.daemon.queue_stream import (
    DeviceQueueStream,
    default_cursor_path,
    read_cursor,
)

BASE = "http://localhost:8000"

_QUEUE_SSE = (
    "event: queue\n"
    "id: 42\n"
    'data: {"items": [{"slug": "atlas", "desired_version": "1.0.0"}], '
    '"device_tasks": [{"id": 7, "task_type": "cli_upgrade", '
    '"payload": {"target_version": "0.6.0"}}]}\n\n'
    "event: ping\n"
    "data: {}\n\n"
)


# ——— транспорт ————————————————————————————————————————————————————


class TestStreamTransport:
    async def test_parses_queue_and_ping_with_cursor(self) -> None:
        """Событие ``queue`` отдаётся с телом и курсором; ``ping`` — как есть."""
        with respx.mock(base_url=BASE) as router:
            route = router.get("/me/devices/queue/stream").mock(
                return_value=Response(
                    200, text=_QUEUE_SSE,
                    headers={"content-type": "text/event-stream"},
                )
            )
            client = HubClient(base_url=BASE, access_token="t")
            events = []
            try:
                async for ev, data, eid in client.stream_device_queue(
                    last_event_id="41", auto_update=True
                ):
                    events.append((ev, data, eid))
            finally:
                await client.close()

        assert [e[0] for e in events] == ["queue", "ping"]
        assert events[0][1]["items"][0]["slug"] == "atlas"
        assert events[0][1]["device_tasks"][0]["task_type"] == "cli_upgrade"
        assert events[0][2] == "42"
        req = route.calls[0].request
        # Курсор — ЗАГОЛОВКОМ; токен — только Authorization, НЕ в query.
        assert req.headers["Last-Event-ID"] == "41"
        assert req.headers["Authorization"] == "Bearer t"
        assert "token" not in str(req.url) and "Bearer" not in str(req.url)
        assert req.headers["Accept"] == "text/event-stream"

    async def test_old_backend_404_raises_api_error_with_status(self) -> None:
        """Старый backend без эндпоинта → ApiError(404) — признак для fallback."""
        from skillery_cli.core.transport import ApiError

        with respx.mock(base_url=BASE) as router:
            router.get("/me/devices/queue/stream").mock(
                return_value=Response(404, json={"detail": "Not Found"})
            )
            client = HubClient(base_url=BASE, access_token="t")
            with pytest.raises(ApiError) as err:
                async for _ in client.stream_device_queue():
                    pass
            await client.close()
        assert err.value.status_code == 404

    async def test_network_break_becomes_transport_error(self) -> None:
        """Обрыв стрима → доменная сетевая ошибка (не сырой httpx-traceback)."""
        from librarykit.errors import TransportError

        with respx.mock(base_url=BASE) as router:
            router.get("/me/devices/queue/stream").mock(
                side_effect=httpx.ConnectError("boom")
            )
            client = HubClient(base_url=BASE, access_token="t")
            with pytest.raises(TransportError):
                async for _ in client.stream_device_queue():
                    pass
            await client.close()


# ——— курсор на диске ————————————————————————————————————————————


class TestCursorPersistence:
    def test_cursor_survives_daemon_restart(self, tmp_path: Path) -> None:
        """Курсор пишется на диск и читается новым объектом (= новый процесс)."""
        p = tmp_path / "device_queue.cursor.json"
        s = DeviceQueueStream(wait=25, cursor_path=p)
        assert s.cursor is None
        s._save_cursor("77")
        assert read_cursor(p) == "77"
        assert DeviceQueueStream(wait=25, cursor_path=p).cursor == "77"

    def test_default_path_lives_next_to_daemon_state(self) -> None:
        from skillery_cli.daemon.daemon_runner import default_state_path

        assert default_cursor_path().parent == default_state_path().parent

    def test_broken_cursor_file_is_not_fatal(self, tmp_path: Path) -> None:
        p = tmp_path / "cursor.json"
        p.write_text("{битый", encoding="utf-8")
        assert read_cursor(p) is None


# ——— такт демона ————————————————————————————————————————————————


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    from skillery_cli.config import ClientConfig

    c = ClientConfig.load()
    c.base_url = BASE
    monkeypatch.setattr(type(c), "effective_store_dir", lambda self: tmp_path / "store")
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    return c


class _Agent:
    name = "claude"


@pytest.fixture
def spy(monkeypatch):  # type: ignore[no-untyped-def]
    """Подменяем ПРИМЕНЕНИЕ очереди — проверяем, что канал зовёт тот же код."""
    import skillery_cli.__main__ as m

    seen: list[dict] = []

    async def _fake(cfg, access, **kw):  # type: ignore[no-untyped-def]
        seen.append(kw)
        return {"applied": [], "failed": [], "skipped": []}

    monkeypatch.setattr(m, "_reconcile_device_queue", _fake)
    monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
    return seen


class TestRunOnce:
    async def test_sse_event_applies_same_queue_payload(
        self, cfg, spy, tmp_path: Path
    ) -> None:
        """(а) Событие SSE → тот же ``_reconcile_device_queue`` с телом события."""
        cur = tmp_path / "cursor.json"
        stream = DeviceQueueStream(wait=25, cursor_path=cur)
        with respx.mock(base_url=BASE) as router:
            router.get("/me/devices/queue/stream").mock(
                return_value=Response(
                    200, text=_QUEUE_SSE,
                    headers={"content-type": "text/event-stream"},
                )
            )
            res = await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )

        assert res == {"mode": "sse", "events": 1}
        assert len(spy) == 1
        payload = spy[0]["payload"]
        assert payload["items"][0]["slug"] == "atlas"
        assert payload["device_tasks"][0]["task_type"] == "cli_upgrade"
        # Клиент переиспользован (SSE-сессия владеет им), long-poll не звался.
        assert spy[0]["client"] is not None
        assert spy[0].get("wait", 0) == 0
        # Курсор ушёл на диск ПОСЛЕ применения.
        assert read_cursor(cur) == "42"

    async def test_reconnect_sends_last_event_id(
        self, cfg, spy, tmp_path: Path
    ) -> None:
        """(б) Разрыв → следующая сессия несёт Last-Event-ID = последний курсор."""
        cur = tmp_path / "cursor.json"
        stream = DeviceQueueStream(wait=25, cursor_path=cur)
        with respx.mock(base_url=BASE) as router:
            route = router.get("/me/devices/queue/stream").mock(
                return_value=Response(
                    200, text=_QUEUE_SSE,
                    headers={"content-type": "text/event-stream"},
                )
            )
            await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )
            # «Разрыв»: сервер закрыл стрим — следующий такт переподключается.
            await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )

        assert len(route.calls) == 2
        assert "last-event-id" not in route.calls[0].request.headers
        assert route.calls[1].request.headers["Last-Event-ID"] == "42"

    async def test_sse_unavailable_falls_back_to_longpoll(self, cfg, spy) -> None:
        """(в) 404 старого backend'а → long-poll в ТОМ ЖЕ такте, демон жив."""
        stream = DeviceQueueStream(wait=25)
        with respx.mock(base_url=BASE) as router:
            sse = router.get("/me/devices/queue/stream").mock(
                return_value=Response(404, json={"detail": "Not Found"})
            )
            res = await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )
            assert res["mode"] == "longpoll"
            # Второй такт даже не пробует SSE (эндпоинта нет — латч навсегда).
            res2 = await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )

        assert res2["mode"] == "longpoll"
        assert len(sse.calls) == 1
        assert stream.mode == "longpoll"
        # Оба такта доставили очередь прежним путём (wait=25, payload не задан).
        assert len(spy) == 2
        assert all(c.get("payload") is None and c["wait"] == 25 for c in spy)

    async def test_network_failure_falls_back_with_backoff(self, cfg, spy) -> None:
        """(в) Сетевой сбой → long-poll + backoff, SSE вернётся позже сам."""
        clock = {"t": 1000.0}
        stream = DeviceQueueStream(
            wait=25, monotonic=lambda: clock["t"]
        )
        with respx.mock(base_url=BASE) as router:
            sse = router.get("/me/devices/queue/stream").mock(
                side_effect=httpx.ConnectError("boom")
            )
            res = await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )
            assert res["mode"] == "longpoll"
            # Внутри окна backoff'а SSE не трогаем — только long-poll.
            clock["t"] += 1.0
            await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )
            assert len(sse.calls) == 1
            assert stream.mode == "longpoll"
            # Окно истекло — пробуем SSE снова (fallback не «навсегда»).
            clock["t"] += 3600.0
            assert stream.mode == "sse"
            await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )
            assert len(sse.calls) == 2
        assert len(spy) == 3  # каждый такт доставлен long-poll'ом

    async def test_ping_only_is_not_a_task(self, cfg, spy, tmp_path: Path) -> None:
        """(г) Heartbeat не задача: очередь не применяется, курсор не двигается."""
        cur = tmp_path / "cursor.json"
        stream = DeviceQueueStream(wait=25, cursor_path=cur)
        with respx.mock(base_url=BASE) as router:
            router.get("/me/devices/queue/stream").mock(
                return_value=Response(
                    200, text="event: ping\ndata: {}\n\n",
                    headers={"content-type": "text/event-stream"},
                )
            )
            res = await stream.run_once(
                cfg, "tok", channel="published", agent_target=_Agent()
            )

        # Ни одного `queue` → сессия пустая, доставку подстраховал long-poll.
        assert res["mode"] == "longpoll"
        assert read_cursor(cur) is None
        assert len(spy) == 1
        assert spy[0].get("payload") is None  # это long-poll, не SSE-применение


# ——— #1438: соединение живёт МЕЖДУ тактами ——————————————————————————


class _LiveStream(httpx.AsyncByteStream):
    """SSE-поток, который НЕ заканчивается — как настоящий сервер.

    Именно на таком потоке виден дефект #1438: сервер держит коннект и шлёт
    heartbeat, а клиент всё равно уходил на переподключение (дедлайн сессии 25с
    проверялся после каждого события, поэтому выход случался на первом ping'е
    после порога — ровно на 30-й секунде).
    """

    def __init__(self, *, head: str = "") -> None:
        self._head = head
        self.pings = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        if self._head:
            yield self._head.encode()
        while True:
            self.pings += 1
            yield b"event: ping\ndata: {}\n\n"
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        return None


def _live_response(stream: _LiveStream) -> Response:
    return Response(
        200, stream=stream, headers={"content-type": "text/event-stream"}
    )


class TestLongLivedSession:
    async def test_session_survives_across_ticks_without_reconnect(
        self, cfg, spy, tmp_path: Path
    ) -> None:
        """#1438: 10 тактов демона = ОДИН коннект, а не десять.

        До правки каждый такт открывал свою сессию и закрывал её на 30-й
        секунде: 422 запроса за 2 часа на проде.
        """
        stream = DeviceQueueStream(wait=25, cursor_path=tmp_path / "c.json")
        body = _LiveStream(head=_QUEUE_SSE.split("event: ping")[0])
        try:
            with respx.mock(base_url=BASE) as router:
                route = router.get("/me/devices/queue/stream").mock(
                    return_value=_live_response(body)
                )
                first = await stream.run_once(
                    cfg, "tok", channel="published", agent_target=_Agent()
                )
                assert first == {"mode": "sse", "live": True}
                for _ in range(9):
                    res = await stream.run_once(
                        cfg, "tok", channel="published", agent_target=_Agent()
                    )
                    assert res["live"] is True
                    assert stream.live
                assert len(route.calls) == 1
        finally:
            await stream.aclose()

    async def test_live_session_does_not_longpoll_every_tick(
        self, cfg, spy, tmp_path: Path
    ) -> None:
        """Пока push жив, опрос — редкая страховка, а не канал доставки."""
        clock = {"t": 1000.0}
        stream = DeviceQueueStream(
            wait=25, cursor_path=tmp_path / "c.json",
            monotonic=lambda: clock["t"],
        )
        body = _LiveStream()
        try:
            with respx.mock(base_url=BASE) as router:
                router.get("/me/devices/queue/stream").mock(
                    return_value=_live_response(body)
                )
                await stream.run_once(
                    cfg, "tok", channel="published", agent_target=_Agent()
                )
                for _ in range(20):
                    clock["t"] += 2.0  # такт демона — раз в 2 секунды
                    await stream.run_once(
                        cfg, "tok", channel="published", agent_target=_Agent()
                    )
                # 40 секунд живого канала — ни одного long-poll'а.
                assert spy == []

                # А вот через 10 минут страховка срабатывает ровно один раз.
                clock["t"] += 601.0
                res = await stream.run_once(
                    cfg, "tok", channel="published", agent_target=_Agent()
                )
                assert res.get("safety_poll") is True
                assert len(spy) == 1
                clock["t"] += 2.0
                await stream.run_once(
                    cfg, "tok", channel="published", agent_target=_Agent()
                )
                assert len(spy) == 1
        finally:
            await stream.aclose()

    async def test_queue_event_is_applied_from_background_session(
        self, cfg, spy, tmp_path: Path
    ) -> None:
        """Задание доезжает БЕЗ участия такта — это и есть push."""
        cur = tmp_path / "c.json"
        stream = DeviceQueueStream(wait=25, cursor_path=cur)
        body = _LiveStream(head=_QUEUE_SSE.split("event: ping")[0])
        try:
            with respx.mock(base_url=BASE) as router:
                router.get("/me/devices/queue/stream").mock(
                    return_value=_live_response(body)
                )
                await stream.run_once(
                    cfg, "tok", channel="published", agent_target=_Agent()
                )
                # Даём фоновой задаче доработать событие.
                for _ in range(50):
                    if spy:
                        break
                    await asyncio.sleep(0.01)
        finally:
            await stream.aclose()

        assert len(spy) == 1
        assert spy[0]["payload"]["items"][0]["slug"] == "atlas"
        assert read_cursor(cur) == "42"

    async def test_reconcile_lock_serialises_with_heavy_pass(
        self, cfg, tmp_path: Path, monkeypatch
    ) -> None:
        """Фоновая сессия и тяжёлый reconcile такта не идут одновременно.

        До #1438 они были последовательны конструктивно (оба внутри одного
        такта); развязка эту гарантию сняла — её возвращает общий замок.
        """
        import skillery_cli.__main__ as m

        overlap = {"max": 0, "cur": 0}

        async def _fake(cfg, access, **kw):  # type: ignore[no-untyped-def]
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
            await asyncio.sleep(0.02)
            overlap["cur"] -= 1
            return {"applied": [], "failed": [], "skipped": []}

        monkeypatch.setattr(m, "_reconcile_device_queue", _fake)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

        stream = DeviceQueueStream(wait=25, cursor_path=tmp_path / "c.json")
        body = _LiveStream(head=_QUEUE_SSE.split("event: ping")[0])
        try:
            with respx.mock(base_url=BASE) as router:
                router.get("/me/devices/queue/stream").mock(
                    return_value=_live_response(body)
                )
                await stream.run_once(
                    cfg, "tok", channel="published", agent_target=_Agent()
                )
                # «Тяжёлый проход» такта под тем же замком.
                async with stream.reconcile_lock:
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.05)
        finally:
            await stream.aclose()

        assert overlap["max"] <= 1
