"""#1191/#1438: PUSH-канал очереди устройства (SSE) с fallback на long-poll.

Зачем: раньше демон ДЕРЖАЛ long-poll — запрос на 25с, ответ, снова запрос. Теперь
основной канал — один живой ``text/event-stream`` (``GET /me/devices/queue/stream``),
по которому сервер сам ПУШИТ события ``queue`` (тело идентично ответу long-poll'а)
и ``ping`` (heartbeat не реже 15с).

#1438 — почему коннект рвался ровно на 30 секундах
--------------------------------------------------
Замер на проде: 422 запроса за 2 часа, p50 длительности 30 078 мс. Причина НЕ в
прокси и НЕ в uvicorn (тот же лог показывает соединения по 8,8 минуты — значит
инфраструктура длинные ответы держит), а ЗДЕСЬ, в клиенте: сессия жила до
``_SESSION_SEC = 25``, а проверка дедлайна стояла ВНУТРИ ``async for`` — то есть
срабатывала только на очередном событии. События же приходят по heartbeat'у раз
в 15с, поэтому выход случался на первом ping'е ПОСЛЕ 25-й секунды, то есть на
30-й. Ровно 30с, детерминированно, на каждом такте.

Дедлайн стоял там потому, что сессия БЛОКИРОВАЛА такт демона, а такту нужно
успевать делать своё (outbox 30с, тяжёлый reconcile 180с). Поэтому лечится не
увеличением дедлайна, а развязкой: SSE-сессия живёт ФОНОВОЙ ЗАДАЧЕЙ
(``asyncio.Task``) через такты, а такт лишь проверяет, что она жива.

Инварианты, которые этот модуль обязан держать:

1. **Задачи не теряются на разрыве.** Курсор последнего ОБРАБОТАННОГО события
   (``id:``) пишется НА ДИСК (``~/.skillery/device_queue.cursor.json``) и при
   переподключении уезжает заголовком ``Last-Event-ID`` — сервер добирает
   пропущенное из БД. Диск, а не память: курсор обязан пережить рестарт демона.
2. **Обработка заданий НЕ продублирована.** Тело события ``queue`` прогоняется
   через ``__main__._reconcile_device_queue(payload=...)`` — тот же код, что и у
   long-poll'а (установка/снятие навыков, ``device_tasks``/``cli_upgrade``,
   рапорты, лимит попыток). Фоновая сессия и тяжёлый reconcile такта разведены
   ``reconcile_lock``: до #1438 они шли последовательно в одном такте, теперь
   могли бы пересечься.
3. **Демон не умирает.** ЛЮБАЯ ошибка SSE — это переход на long-poll в ЭТОМ ЖЕ
   такте (доставка не проседает) плюс backoff перед следующей попыткой SSE. Не
   исключение наружу.
4. **Такт демона не блокируется.** Сессия ушла в фон; такт ждёт её только до
   первого события (``_FIRST_EVENT_GRACE_SEC``) — чтобы сбой «на подключении»
   по-прежнему давал fallback в ТОМ ЖЕ такте, а не через один.
5. **Polling остаётся, но редким.** Пока SSE жив, long-poll делается раз в
   ``_SAFETY_POLL_SEC`` (10 минут) — страховка от потерянного сигнала, а не
   основной канал.

Признаки fallback'а:

- ``404``/``405``/``501`` на стриме = старого backend'а без эндпоинта — SSE
  выключается НАВСЕГДА в этом процессе (пробовать больше нечего);
- любая другая ошибка (сеть, прокси режет stream, обрыв до первого события) —
  временный отвод: экспоненциальный backoff ``30с → 60с → … → 15мин``, всё это
  время работает long-poll, потом снова пробуем SSE.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from skillery_cli.core import route_health

# #1438: потолок ЖИЗНИ одной сессии. Это гигиена (свежий токен, чистый TCP,
# защита от полудохлого коннекта после сна ноутбука), а НЕ ритм доставки: такт
# демона сессию больше не ждёт, она живёт фоновой задачей. Полчаса вместо
# прежних 25 секунд — в 72 раза меньше переподключений.
_SESSION_SEC = 1800.0
# Сколько такт ждёт ПЕРВОГО события новой сессии, прежде чем считать её живой.
# Сервер отдаёт снапшот сразу при подключении, поэтому это доли секунды; потолок
# нужен лишь чтобы сбой «на подключении» дал fallback в ТОМ ЖЕ такте (инвариант 3).
_FIRST_EVENT_GRACE_SEC = 10.0
# #1438: пока SSE жив, long-poll остаётся РЕДКОЙ страховкой (потерянный сигнал,
# рассинхрон), а не каналом доставки.
_SAFETY_POLL_SEC = 600.0
# Heartbeat сервера — не реже 15с. Таймаут чтения ставим ВЫШЕ: молчание дольше
# него = мёртвый коннект. Задаётся на КОНСТРУКЦИИ HubClient (транспорт кита не
# принимает per-request timeout — на этом уже горели, см. #1102).
_READ_TIMEOUT_SEC = 35.0

_BACKOFF_BASE_SEC = 30.0
_BACKOFF_MAX_SEC = 900.0

# Старый backend / эндпоинта нет — повторять бессмысленно.
_UNSUPPORTED_STATUSES = frozenset({404, 405, 501})


def default_cursor_path() -> Path:
    """Файл курсора SSE — рядом с прочим состоянием демона (``~/.skillery``)."""
    from skillery_cli.daemon.daemon_runner import _default_data_dir

    return _default_data_dir() / "device_queue.cursor.json"


def read_cursor(path: Path | None = None) -> str | None:
    """Последний ОБРАБОТАННЫЙ курсор очереди (или ``None``). Битый файл = None."""
    p = path or default_cursor_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый → начинаем без курсора
        return None
    value = data.get("last_event_id") if isinstance(data, dict) else None
    return str(value) if value else None


def write_cursor(value: str, path: Path | None = None) -> None:
    """Сохранить курсор (best-effort: сбой записи не повод валить демон)."""
    p = path or default_cursor_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"last_event_id": str(value)}), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — курсор best-effort, дубль лучше падения
        pass


class DeviceQueueStream:
    """Один канал доставки очереди устройства: SSE, с откатом на long-poll.

    Живёт столько же, сколько цикл демона (создаётся в ``_build_runner``), и
    держит между тактами: курсор, признак «SSE не поддержан», окно backoff'а.

    ``run_once`` — ровно один такт доставки: либо SSE-сессия (до ``_SESSION_SEC``),
    либо long-poll. Возвращает ``{"mode": "sse"|"longpoll", "events": N}`` —
    источник правды для тестов и логов.
    """

    def __init__(
        self,
        *,
        wait: int,
        cursor_path: Path | None = None,
        session_sec: float = _SESSION_SEC,
        reconcile_lock: asyncio.Lock | None = None,
        monotonic=time.monotonic,  # noqa: ANN001 — точка подмены в тестах
    ) -> None:
        self._wait = wait
        self._cursor_path = cursor_path
        self._session_sec = session_sec
        self._monotonic = monotonic
        self._cursor: str | None = read_cursor(cursor_path)
        self._sse_unsupported = False
        self._retry_not_before = 0.0
        self._failures = 0
        self._logged_fallback = False
        # #1438: живая сессия переживает такты. Раньше её не существовало как
        # объекта вовсе — сессия была локальной переменной внутри run_once.
        self._task: asyncio.Task[int] | None = None
        self._first_event = asyncio.Event()
        self._last_safety_poll = 0.0
        # #1438: фоновая сессия применяет очередь ПАРАЛЛЕЛЬНО такту демона, а
        # такт делает тяжёлый reconcile (device-sync, auto-update). До развязки
        # они шли друг за другом в одном такте и пересечься не могли; теперь
        # могут — и оба ставят/снимают навыки. Замок ОБЩИЙ и его владелец —
        # такт (он же его и передаёт): координация принадлежит тому, кто
        # координирует, а не одному из координируемых.
        self.reconcile_lock = reconcile_lock or asyncio.Lock()

    # ——— состояние ————————————————————————————————————————————————

    @property
    def mode(self) -> str:
        """Каким каналом пойдёт СЛЕДУЮЩИЙ такт (для status/логов)."""
        return "longpoll" if not self._sse_enabled_now() else "sse"

    @property
    def cursor(self) -> str | None:
        return self._cursor

    @property
    def live(self) -> bool:
        """#1438: сессия SSE прямо сейчас держит коннект."""
        return self._task is not None and not self._task.done()

    def _sse_enabled_now(self) -> bool:
        if self._sse_unsupported:
            return False
        return self._monotonic() >= self._retry_not_before

    def _note_success(self) -> None:
        self._failures = 0
        self._retry_not_before = 0.0
        self._logged_fallback = False

    def _note_failure(self, log: Any, exc: BaseException) -> None:
        """Временный отвод на long-poll с экспоненциальным backoff'ом."""
        self._failures += 1
        delay = min(
            _BACKOFF_BASE_SEC * (2 ** (self._failures - 1)), _BACKOFF_MAX_SEC
        )
        self._retry_not_before = self._monotonic() + delay
        with suppress(Exception):
            log.warning(
                "SSE-очередь недоступна — работаем long-poll'ом",
                extra={"context": {
                    "channel": "sse", "fallback": "longpoll",
                    "failures": self._failures, "retry_in": delay,
                    "error": str(exc) or repr(exc),
                    "error_type": type(exc).__name__,
                }},
            )

    @staticmethod
    def _stream_route() -> str:
        """Путь push-канала — тот же, что строит транспорт (ключ route_health)."""
        from skillery_cli.core.identity import device_uid

        return f"/devices/{device_uid()}/tasks/stream"

    def _note_unsupported(self, log: Any, status: int | None) -> None:
        """Backend без эндпоинта — навсегда long-poll (в этом процессе).

        #1479: 404/405 здесь — не только «старый сервер», но и **расхождение
        контракта** (путь стрима переименован). Fallback на long-poll спасает
        доставку лишь пока жив второй путь; при переезде обоих
        (``/me/device-queue`` → ``/devices/{cdid}/tasks``) молчаливый латч
        превращал рассинхрон в «устройство просто офлайн». Поэтому поднимаем тот
        же видимый флаг, что и long-poll — его печатает ``skillery status``.
        """
        self._sse_unsupported = True
        if status in route_health.CONTRACT_STATUSES:
            with suppress(Exception):
                route_health.record_unknown_route(
                    "GET", self._stream_route(), int(status), source="daemon.sse"
                )
        if not self._logged_fallback:
            self._logged_fallback = True
            with suppress(Exception):
                log.warning(
                    "SSE-очередь не поддержана сервером — постоянный long-poll",
                    extra={"context": {
                        "channel": "sse", "fallback": "longpoll",
                        "status": status,
                    }},
                )

    def _save_cursor(self, event_id: str) -> None:
        self._cursor = str(event_id)
        write_cursor(self._cursor, self._cursor_path)

    # ——— такт доставки ————————————————————————————————————————————

    async def aclose(self) -> None:
        """Погасить фоновую сессию (остановка демона)."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(BaseException):
            await task

    def _harvest(self, log: Any) -> int | None:
        """Разобрать ЗАВЕРШЁННУЮ сессию: успех/провал/«сервер не умеет».

        Возвращает число применённых событий (``None`` — сессия упала). Именно
        здесь живёт классификация из инварианта 3; она переехала сюда из
        ``run_once`` без изменений, потому что теперь исход приходит от задачи,
        а не от прямого await'а.
        """
        task, self._task = self._task, None
        if task is None:
            return None
        try:
            handled = task.result()
        except asyncio.CancelledError:
            return None
        except (KeyboardInterrupt, SystemExit):
            # Остановку демона глотать нельзя — её ждёт супервизор.
            raise
        except BaseException as exc:  # noqa: BLE001 — SSE не имеет права валить демон
            status = getattr(exc, "status_code", None)
            if status in _UNSUPPORTED_STATUSES:
                self._note_unsupported(log, status)
            else:
                self._note_failure(log, exc)
            return None
        self._note_success()
        return handled

    def _due_safety_poll(self) -> bool:
        """#1438: пора ли делать РЕДКУЮ страховочную выборку очереди."""
        now = self._monotonic()
        if now - self._last_safety_poll < _SAFETY_POLL_SEC:
            return False
        self._last_safety_poll = now
        return True

    async def run_once(
        self, cfg, access: str, *, channel: str, agent_target
    ) -> dict[str, Any]:
        """Один такт доставки.

        #1438: такт больше НЕ равен сессии. Если фоновая сессия жива — такт
        ничего не делает (события приезжают сами, push'ем) и лишь изредка
        подстраховывается long-poll'ом. Если сессии нет — поднимаем её и ждём
        первого события, чтобы сбой на подключении по-прежнему давал fallback
        в ЭТОМ ЖЕ такте (инвариант 3), а не через один.
        """
        from skillery_cli.core.logging_setup import get_logger

        log = get_logger("reconcile")

        if self.live:
            # Коннект держится — доставка идёт push'ем, ждать нечего.
            if self._due_safety_poll():
                await self._run_longpoll(
                    cfg, access, channel=channel, agent_target=agent_target
                )
                return {"mode": "sse", "live": True, "safety_poll": True}
            return {"mode": "sse", "live": True}

        if self._sse_enabled_now():
            self._first_event = asyncio.Event()
            self._task = asyncio.ensure_future(
                self._run_sse_session(
                    cfg, access, channel=channel, agent_target=agent_target,
                    log=log,
                )
            )
            # Ждём ЛИБО первое событие (канал ожил), ЛИБО завершение задачи
            # (упала или сервер закрыл стрим) — что случится раньше.
            await self._await_first_event()
            if self.live:
                self._last_safety_poll = self._monotonic()
                return {"mode": "sse", "live": True}
            handled = self._harvest(log)
            # Хотя бы одно событие обработано ⇒ доставка состоялась,
            # long-poll в этом такте не нужен (иначе двойной проход).
            if handled:
                return {"mode": "sse", "events": handled}
            # Пустая сессия (сервер закрыл без событий) — подстрахуемся
            # long-poll'ом: доставка не имеет права проседать.
        return await self._run_longpoll(
            cfg, access, channel=channel, agent_target=agent_target
        )

    async def _await_first_event(self) -> None:
        """Дождаться первого события новой сессии (или её падения)."""
        task = self._task
        if task is None:
            return
        waiter = asyncio.ensure_future(self._first_event.wait())
        try:
            await asyncio.wait(
                {task, waiter},
                timeout=_FIRST_EVENT_GRACE_SEC,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()
            with suppress(BaseException):
                await waiter

    async def _run_sse_session(
        self, cfg, access: str, *, channel: str, agent_target, log
    ) -> int:
        """Открыть стрим и применить пришедшие ``queue``-события. → сколько применили.

        ``ping`` — heartbeat, НЕ задача: продлевает сессию, но ничего не
        применяет и курсор не двигает.

        #1438: живёт до ``_SESSION_SEC`` (полчаса), а не до 25 секунд, и дедлайн
        проверяется ПОСЛЕ обработки события — то есть фактический выход
        случается на первом heartbeat'е за порогом. Раньше при пороге 25с и
        heartbeat'е 15с это давало ровно 30 секунд на каждой сессии; теперь
        квантование в 15с к получасу несущественно.
        """
        import skillery_cli.__main__ as main_mod
        from skillery_cli.core.transport import HubClient

        client = HubClient(
            base_url=cfg.base_url, access_token=access,
            on_token_refresh=main_mod._make_refresh_callback(cfg),
            timeout=_READ_TIMEOUT_SEC,
        )
        deadline = self._monotonic() + self._session_sec
        handled = 0
        try:
            stream = client.stream_device_queue(
                last_event_id=self._cursor,
                auto_update=cfg.auto_update,
                supports_removal=True,  # #13: этот CLI умеет снимать навыки
            )
            try:
                async for event, data, event_id in stream:
                    # Канал ожил — такт может отпускать сессию в фон.
                    self._first_event.set()
                    # #1479: и снимаем флаг рассинхрона, если он был поднят
                    # прошлой (устаревшей) версией CLI — «починилось после
                    # апдейта» не должно требовать ручной уборки.
                    with suppress(Exception):
                        route_health.record_ok("GET", self._stream_route())
                    if event == "queue":
                        # #1438: замок против тяжёлого reconcile такта — они
                        # больше не последовательны (сессия ушла в фон).
                        async with self.reconcile_lock:
                            await main_mod._reconcile_device_queue(
                                cfg, access, channel=channel,
                                agent_target=agent_target,
                                payload=data, client=client,
                            )
                        handled += 1
                        # Курсор двигаем ТОЛЬКО после применения: упади мы
                        # раньше — сервер переотдаст это же событие.
                        if event_id:
                            self._save_cursor(event_id)
                    elif event == "ping":
                        with suppress(Exception):
                            log.debug("SSE heartbeat", extra={"context": {
                                "channel": "sse", "event": "ping"}})
                    if self._monotonic() >= deadline:
                        break
            finally:
                # Явное закрытие генератора: иначе на Windows стрим мог бы
                # дожить до GC уже после закрытия клиента.
                with suppress(Exception):
                    await stream.aclose()
        finally:
            await client.close()
        return handled

    async def _run_longpoll(
        self, cfg, access: str, *, channel: str, agent_target
    ) -> dict[str, Any]:
        """Прежний путь: long-poll ``/me/device-queue`` (ничего не изменилось)."""
        import skillery_cli.__main__ as main_mod

        await main_mod._reconcile_device_queue(
            cfg, access, channel=channel, agent_target=agent_target,
            wait=self._wait,
        )
        return {"mode": "longpoll", "events": 0}
