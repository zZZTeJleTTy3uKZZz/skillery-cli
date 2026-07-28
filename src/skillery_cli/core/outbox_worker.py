"""#1174: воркер доставки ОБЩЕГО outbox'а (``telemetrykit.outbox``) на бэкенд.

Зачем это отдельный слой
------------------------
Локальные процессы, которым надо что-то доставить в хаб, сетью НЕ ходят: навык
пишет ``kind="skill_run"`` декоратором ``@track_skill()``, лог-синк CLI пишет
``kind="log"`` (см. :mod:`skillery_cli.core.log_sync`) — оба просто дописывают
конверт в ``~/.skillery/outbox.jsonl``. Сеть — здесь, в ОДНОМ месте, и зовётся
она из цикла демона (``commands/daemon.py::_build_runner``).

Очередь ОДНА намеренно. Две очереди — это две разные гарантии доставки, два
места, где записи теряются, и два повода объяснять пользователю, почему в вебе
видно одно, а на машине лежит другое. Поэтому C3-очередь логов
(``logs/sync.queue.jsonl``) сюда перелита, а заводить новую — нельзя.

Контракт бэкенда (``POST /telemetry/batch``)::

    {"envelopes": [{"id", "kind", "ts", "schema_version", "payload"}, ...]}
    → {"accepted": ["id", ...], "rejected": [{"id": "...", "reason": "..."}]}

Из outbox'а удаляются И ``accepted``, И ``rejected``: отклонённый конверт от
повтора валидным не станет, а очередь, вставшая на одном битом конверте, теряет
ВСЮ остальную телеметрию — поэтому ``rejected`` уходят с WARNING (факт потери
виден), но уходят.

Инварианты
----------
- **Ничего не теряем при офлайне.** Сбой сети/5xx/таймаут ⇒ конверты остаются
  в файле, ack не делается; повтор — следующим проходом.
- **Не зацикливаемся.** 4xx на ВЕСЬ батч (кроме «повторяемых»: 401 протухший
  токен, 429 rate-limit и т.п.) означает «тело невалидно» — батч уходит в
  карантин (удаляется) с WARNING, иначе очередь встанет намертво.
- **Не долбим мёртвый бэкенд.** Экспоненциальный backoff (30с → 15 мин), он же
  гасит и ``force``: принуждать имеет смысл троттл, а не отказ сети.
- **Не роняем демон.** :func:`flush_outbox_safe` не выпускает наверх ничего,
  кроме ``CancelledError``.
- **Не кормим сами себя.** Логгер воркера (``skillery.outbox``) исключён из
  лог-синка: иначе каждая ошибка доставки рождала бы новый конверт, и очередь
  в офлайне росла бы сама от себя.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from skillery_cli.core.logging_setup import get_logger

#: Имя логгера воркера (подсистема) и его ПОЛНОЕ имя в дереве ``skillery``.
#: Вынесено в константы, потому что на полное имя завязан анти-петлевой фильтр
#: лог-синка (см. ``log_sync.LogSyncHandler.emit``): доставщик не имеет права
#: писать в очередь, которую сам же и везёт.
LOGGER_NAME = "outbox"
LOGGER_NAME_FULL = "skillery.outbox"

#: Сколько конвертов уходит одним ``POST /telemetry/batch``.
BATCH_LIMIT = 50
#: Жёсткий таймаут одной отправки — доставка не держит цикл демона/команду.
FLUSH_TIMEOUT_SEC = 10.0
#: Троттл: не чаще раза в N секунд в ОДНОМ процессе (``force`` пробивает).
MIN_FLUSH_INTERVAL_SEC = 30.0

#: Экспоненциальный backoff при сетевых сбоях/5xx.
BACKOFF_BASE_SEC = 30.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_SEC = 900.0

#: 4xx, которые НЕ означают «конверт невалиден» — их повторяем.
#: 401 — протух access (refresh даст новый); 403 — прав может не быть временно
#: (сменилась компания); 408/425 — таймаут/too-early; 409 — гонка; 423 — locked;
#: 429 — rate-limit (ровно то, ради чего есть backoff).
RETRYABLE_STATUSES = frozenset({401, 403, 408, 409, 423, 425, 429})

_log = get_logger(LOGGER_NAME)

# Процессное состояние троттла/backoff. Модульное, а не в объекте, потому что
# точек вызова несколько (цикл демона, login, finally у install), а окно
# «не чаще раза в N секунд» — общее на процесс.
_LAST_FLUSH_AT = 0.0
_BACKOFF_UNTIL = 0.0
_BACKOFF_DELAY = 0.0


@dataclass(frozen=True)
class OutboxFlushResult:
    """Исход одного прохода доставки (для логов/тестов, не для UI)."""

    read: int = 0
    accepted: int = 0
    rejected: int = 0
    removed: int = 0
    skipped: bool = False
    error: str | None = None


def reset_throttle() -> None:
    """Сбросить троттл и backoff (тесты / принудительная досылка)."""
    global _LAST_FLUSH_AT, _BACKOFF_UNTIL, _BACKOFF_DELAY
    _LAST_FLUSH_AT = 0.0
    _BACKOFF_UNTIL = 0.0
    _BACKOFF_DELAY = 0.0


def backoff_delay() -> float:
    """Текущая пауза backoff'а (0.0 — сбоев нет)."""
    return _BACKOFF_DELAY


def outbox_path():  # noqa: ANN201 — Path, но импорт кита ленивый
    """Путь к общему outbox'у (диагностика ``daemon status`` / ``doctor``)."""
    from telemetrykit import outbox

    return outbox.path()


def is_due(
    *,
    force: bool = False,
    min_interval: float = MIN_FLUSH_INTERVAL_SEC,
    now: float | None = None,
) -> bool:
    """Пора ли идти по сети.

    Отдельная от :func:`flush_outbox` функция намеренно: цикл демона спрашивает
    ДО построения HTTP-клиента — незачем поднимать клиент, чтобы тут же его
    закрыть.

    ``force`` пробивает троттл, но НЕ backoff: принуждать имеет смысл окно
    «не чаще раза в N секунд», а не отказ сети.
    """
    moment = time.monotonic() if now is None else now
    if _BACKOFF_UNTIL and moment < _BACKOFF_UNTIL:
        return False
    if force:
        return True
    return not (_LAST_FLUSH_AT and (moment - _LAST_FLUSH_AT) < min_interval)


def _record_failure(now: float) -> None:
    global _BACKOFF_DELAY, _BACKOFF_UNTIL
    _BACKOFF_DELAY = min(
        BACKOFF_MAX_SEC,
        (_BACKOFF_DELAY * BACKOFF_FACTOR) if _BACKOFF_DELAY else BACKOFF_BASE_SEC,
    )
    _BACKOFF_UNTIL = now + _BACKOFF_DELAY


def _record_success() -> None:
    global _BACKOFF_DELAY, _BACKOFF_UNTIL
    _BACKOFF_DELAY = 0.0
    _BACKOFF_UNTIL = 0.0


def _status_of(exc: BaseException) -> int | None:
    """HTTP-статус из ошибки транспорта (``ApiError.status_code``), если он есть.

    По утке, а не по ``isinstance``: воркер не обязан знать конкретный класс
    ошибки клиента (в тестах и у альтернативных транспортов он свой).
    """
    status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _split_response(
    response: Any, envelopes: list[dict[str, Any]]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Ответ бэкенда → (accepted ids, rejected записи).

    Терпимо к форме: старый/урезанный бэкенд, вернувший не-словарь или без
    ключей, даёт пустые списки — тогда ничего не удаляется и конверты уедут
    следующим проходом (лучше дубль на бэке, чем молчаливая потеря).
    """
    if not isinstance(response, dict):
        return [], []
    known = {str(e.get("id")) for e in envelopes}
    accepted = [
        str(i) for i in (response.get("accepted") or []) if str(i) in known
    ]
    rejected: list[dict[str, Any]] = []
    for item in response.get("rejected") or []:
        if isinstance(item, dict) and str(item.get("id")) in known:
            rejected.append(item)
        elif isinstance(item, str) and item in known:
            rejected.append({"id": item, "reason": ""})
    return accepted, rejected


async def flush_outbox(
    client: Any,
    *,
    limit: int = BATCH_LIMIT,
    timeout: float = FLUSH_TIMEOUT_SEC,
    force: bool = False,
    min_interval: float = MIN_FLUSH_INTERVAL_SEC,
) -> OutboxFlushResult:
    """Один проход доставки: ``read_batch`` → ``POST /telemetry/batch`` → ``remove``.

    Возвращает :class:`OutboxFlushResult`; наверх бросает только
    ``CancelledError`` (см. :func:`flush_outbox_safe`, если и это нежелательно).
    """
    global _LAST_FLUSH_AT

    from telemetrykit import outbox

    now = time.monotonic()
    if not is_due(force=force, min_interval=min_interval, now=now):
        return OutboxFlushResult(skipped=True)

    try:
        envelopes = outbox.read_batch(limit)
    except Exception as exc:  # noqa: BLE001 — битый файл не валит демон
        _log.error("outbox: очередь не читается", extra={"context": {
            "error": str(exc) or repr(exc), "error_type": type(exc).__name__,
        }})
        return OutboxFlushResult(error=str(exc) or repr(exc))

    _LAST_FLUSH_AT = now
    if not envelopes:
        _record_success()  # пустая очередь = связь ни при чём, backoff снимаем
        return OutboxFlushResult()

    send = getattr(client, "send_telemetry_batch", None)
    if send is None:
        # Старый/урезанный клиент — НЕ теряем: доедет, когда появится метод.
        return OutboxFlushResult(read=len(envelopes))

    try:
        response = await asyncio.wait_for(send(envelopes), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — доставка не валит вызывающего
        return _handle_send_failure(exc, envelopes, now=now)

    accepted, rejected = _split_response(response, envelopes)
    ack = accepted + [str(r.get("id")) for r in rejected]
    removed = 0
    if ack:
        try:
            removed = outbox.remove(ack)
        except Exception as exc:  # noqa: BLE001 — не смогли усечь → повтор/дубль
            _log.error("outbox: ack не записался", extra={"context": {
                "error": str(exc) or repr(exc), "ids": len(ack),
            }})
    _record_success()

    if rejected:
        # WARNING, а не ERROR: доставка исправна, невалиден конверт. Причина
        # обязана быть в логе — иначе потеря данных становится невидимой.
        _log.warning(
            "outbox: конверты отклонены бэкендом — удалены (повтор их не исправит)",
            extra={"context": {
                "rejected": len(rejected),
                "reasons": [str(r.get("reason") or "")[:200] for r in rejected[:5]],
                "kinds": sorted({str(e.get("kind")) for e in envelopes
                                 if str(e.get("id")) in
                                 {str(r.get("id")) for r in rejected}}),
            }},
        )
    if accepted:
        # Успех — INFO (не ERROR): на стандартном уровне лог не шумит, а при
        # разборе видно, сколько и чего реально уехало.
        _log.info("outbox: батч доставлен", extra={"context": {
            "read": len(envelopes), "accepted": len(accepted),
            "rejected": len(rejected), "removed": removed,
        }})
    return OutboxFlushResult(
        read=len(envelopes),
        accepted=len(accepted),
        rejected=len(rejected),
        removed=removed,
    )


def _handle_send_failure(
    exc: BaseException, envelopes: list[dict[str, Any]], *, now: float
) -> OutboxFlushResult:
    """Разобрать сбой отправки: карантин 4xx vs backoff сети/5xx."""
    from telemetrykit import outbox

    reason = str(exc) or repr(exc)
    status = _status_of(exc)
    fatal = (
        status is not None
        and 400 <= status < 500
        and status not in RETRYABLE_STATUSES
    )
    if fatal:
        # Тело батча бэкенд не принимает В ПРИНЦИПЕ — повтор даст ту же 4xx, а
        # очередь встанет намертво и похоронит всю последующую телеметрию.
        ids = [str(e.get("id")) for e in envelopes]
        removed = 0
        try:
            removed = outbox.remove(ids)
        except Exception:  # noqa: BLE001
            removed = 0
        _log.warning(
            "outbox: батч отвергнут (4xx) — карантин, повторять бессмысленно",
            extra={"context": {
                "status": status, "error": reason[:500], "removed": removed,
                "kinds": sorted({str(e.get("kind")) for e in envelopes}),
            }},
        )
        _record_success()  # сеть-то жива — backoff не про этот случай
        return OutboxFlushResult(
            read=len(envelopes), rejected=len(ids), removed=removed, error=reason
        )

    _record_failure(now)
    # ERROR, но БЕЗ exc_info: офлайн — штатное состояние ноутбука, а полный
    # трейс на каждый сбой сети делает лог нечитаемым.
    _log.error("outbox: батч не доставлен — повтор позже", extra={"context": {
        "status": status,
        "error": reason[:500] or type(exc).__name__,
        "error_type": type(exc).__name__,
        "pending": len(envelopes),
        "retry_in": _BACKOFF_DELAY,
    }})
    return OutboxFlushResult(read=len(envelopes), error=reason or type(exc).__name__)


async def flush_outbox_safe(client: Any, **kwargs: Any) -> OutboxFlushResult:
    """:func:`flush_outbox`, который не бросает ничего (кроме отмены задачи)."""
    try:
        return await flush_outbox(client, **kwargs)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — доставка не валит демон
        return OutboxFlushResult(error=str(exc) or repr(exc))


__all__ = [
    "BACKOFF_BASE_SEC",
    "BACKOFF_MAX_SEC",
    "BATCH_LIMIT",
    "FLUSH_TIMEOUT_SEC",
    "LOGGER_NAME",
    "LOGGER_NAME_FULL",
    "MIN_FLUSH_INTERVAL_SEC",
    "RETRYABLE_STATUSES",
    "OutboxFlushResult",
    "backoff_delay",
    "flush_outbox",
    "flush_outbox_safe",
    "is_due",
    "outbox_path",
    "reset_throttle",
]
