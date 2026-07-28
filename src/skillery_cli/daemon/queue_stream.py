"""#1191: PUSH-канал очереди устройства (SSE) с прозрачным fallback на long-poll.

Зачем: раньше демон ДЕРЖАЛ long-poll — запрос на 25с, ответ, снова запрос. Теперь
основной канал — один живой ``text/event-stream`` (``GET /me/devices/queue/stream``),
по которому сервер сам ПУШИТ события ``queue`` (тело идентично ответу long-poll'а)
и ``ping`` (heartbeat не реже 20с).

Инварианты, которые этот модуль обязан держать:

1. **Задачи не теряются на разрыве.** Курсор последнего ОБРАБОТАННОГО события
   (``id:``) пишется НА ДИСК (``~/.skillery/device_queue.cursor.json``) и при
   переподключении уезжает заголовком ``Last-Event-ID`` — сервер добирает
   пропущенное из БД. Диск, а не память: курсор обязан пережить рестарт демона.
2. **Обработка заданий НЕ продублирована.** Тело события ``queue`` прогоняется
   через ``__main__._reconcile_device_queue(payload=...)`` — тот же код, что и у
   long-poll'а (установка/снятие навыков, ``device_tasks``/``cli_upgrade``,
   рапорты, лимит попыток).
3. **Демон не умирает.** ЛЮБАЯ ошибка SSE — это переход на long-poll в ЭТОМ ЖЕ
   такте (доставка не проседает) плюс backoff перед следующей попыткой SSE. Не
   исключение наружу.
4. **Такт демона не блокируется навсегда.** Сессия ограничена по времени
   (``_SESSION_SEC``) — ровно как прежний long-poll, чтобы каденсы такта
   (outbox 30с, тяжёлый reconcile 180с) остались прежними.

Признаки fallback'а:

- ``404``/``405``/``501`` на стриме = старого backend'а без эндпоинта — SSE
  выключается НАВСЕГДА в этом процессе (пробовать больше нечего);
- любая другая ошибка (сеть, прокси режет stream, обрыв до первого события) —
  временный отвод: экспоненциальный backoff ``30с → 60с → … → 15мин``, всё это
  время работает long-poll, потом снова пробуем SSE.
"""
from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

# Сессию держим столько же, сколько прежний long-poll (25с): такт демона гоняет
# outbox (30с) и тяжёлый reconcile (180с) — растянутая SSE-сессия сдвинула бы их.
_SESSION_SEC = 25.0
# Heartbeat сервера — не реже 20с. Таймаут чтения ставим ВЫШЕ: молчание дольше
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

    # ——— состояние ————————————————————————————————————————————————

    @property
    def mode(self) -> str:
        """Каким каналом пойдёт СЛЕДУЮЩИЙ такт (для status/логов)."""
        return "longpoll" if not self._sse_enabled_now() else "sse"

    @property
    def cursor(self) -> str | None:
        return self._cursor

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

    def _note_unsupported(self, log: Any, status: int | None) -> None:
        """Backend без эндпоинта — навсегда long-poll (в этом процессе)."""
        self._sse_unsupported = True
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

    async def run_once(
        self, cfg, access: str, *, channel: str, agent_target
    ) -> dict[str, Any]:
        """Один такт: SSE-сессия, а если не вышло — long-poll (без регрессии)."""
        from skillery_cli.core.logging_setup import get_logger

        log = get_logger("reconcile")
        if self._sse_enabled_now():
            try:
                handled = await self._run_sse_session(
                    cfg, access, channel=channel, agent_target=agent_target,
                    log=log,
                )
            except (KeyboardInterrupt, SystemExit):
                # Остановку демона глотать нельзя — её ждёт супервизор.
                raise
            except BaseException as exc:  # noqa: BLE001 — SSE не имеет права валить демон
                status = getattr(exc, "status_code", None)
                if status in _UNSUPPORTED_STATUSES:
                    self._note_unsupported(log, status)
                else:
                    self._note_failure(log, exc)
            else:
                self._note_success()
                # Хотя бы одно событие обработано ⇒ доставка состоялась,
                # long-poll в этом такте не нужен (иначе двойной проход).
                if handled:
                    return {"mode": "sse", "events": handled}
                # Пустая сессия (сервер закрыл без событий) — подстрахуемся
                # long-poll'ом: доставка не имеет права проседать.
        return await self._run_longpoll(
            cfg, access, channel=channel, agent_target=agent_target
        )

    async def _run_sse_session(
        self, cfg, access: str, *, channel: str, agent_target, log
    ) -> int:
        """Открыть стрим и применить пришедшие ``queue``-события. → сколько применили.

        ``ping`` — heartbeat, НЕ задача: продлевает сессию, но ничего не
        применяет и курсор не двигает.
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
                    if event == "queue":
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
