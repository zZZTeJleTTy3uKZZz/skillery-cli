"""#1174/#1180: воркер доставки ОБЩЕГО outbox'а (``telemetrykit.outbox``) на бэкенд.

Зачем это отдельный слой
------------------------
Локальные процессы, которым надо что-то доставить в хаб, сетью НЕ ходят: навык
пишет ``kind="skill_run"`` декоратором телеметрии, лог-синк CLI пишет
``kind="log"`` (см. :mod:`skillery_cli.core.log_sync`), инструментация установок
пишет ``kind="analytics_event"`` (см. :mod:`skillery_cli.core.analytics_sync`) —
все просто дописывают конверт в общий JSONL. Сеть — здесь, в ОДНОМ месте, и
зовётся она из цикла демона (``commands/daemon.py::_build_runner``).

Очередь ОДНА намеренно. Две (а до #1180 — три) очереди — это разные гарантии
доставки, разные места, где записи теряются, и лишний повод объяснять
пользователю, почему в вебе видно одно, а на машине лежит другое. Поэтому
C3-очередь логов (``logs/sync.queue.jsonl``, #1174) и очередь аналитики
(``events.queue.json``, #1180) сюда перелиты, а заводить новую — нельзя.

Одна очередь ≠ один эндпоинт
----------------------------
ХРАНИЛИЩЕ общее, а ветка отправки выбирается по ``kind``:

- ``kind="analytics_event"`` → ``POST /events`` (``HubClient.ingest_events``).
  Этот эндпоинт принимает АНОНИМНО (backend ``_optional_claims``), и терять это
  нельзя: событие установки рождается и на машине, где никто не логинился
  (``install --path`` / ``--from-git``), и при протухшей сессии — а именно на
  них стоит метрика активаций. В ``/telemetry/batch`` (Bearer обязателен) они бы
  вечно висели в очереди: 401 — статус повторяемый, значит конверты не ушли бы
  никогда, и метрика просто исчезла бы. Поэтому при 401 ветка ретраит тот же
  батч БЕЗ Bearer — ровно как это делал прежний ``EventSender``.
- всё остальное → ``POST /telemetry/batch`` (``send_telemetry_batch``).

Бэкенд от переезда аналитики не меняется вообще: контракт ``/events`` тот же.

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
- **Ветки не блокируют друг друга.** Сбой одной не мешает ack другой, а окно
  чтения шире батча (:data:`READ_OVERSCAN`), чтобы залипший ``kind`` не
  застревал в голове очереди и не морил голодом остальные.
- **Не долбим мёртвый бэкенд.** Экспоненциальный backoff (30с → 15 мин), он же
  гасит и ``force``: принуждать имеет смысл троттл, а не отказ сети. Backoff
  растёт, только если проход НЕ ПРОДВИНУЛСЯ вовсе (ничего не удалилось): пока
  хоть что-то уезжает, связь жива и тормозить всю очередь нечестно.
- **Не роняем демон.** :func:`flush_outbox_safe` не выпускает наверх ничего,
  кроме ``CancelledError``.
- **Не кормим сами себя.** Логгер воркера (``skillery.outbox``) исключён из
  лог-синка: иначе каждая ошибка доставки рождала бы новый конверт, и очередь
  в офлайне росла бы сама от себя.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from skillery_cli.core.analytics_sync import KIND_ANALYTICS_EVENT
from skillery_cli.core.logging_setup import get_logger

#: Имя логгера воркера (подсистема) и его ПОЛНОЕ имя в дереве ``skillery``.
#: Вынесено в константы, потому что на полное имя завязан анти-петлевой фильтр
#: лог-синка (см. ``log_sync.LogSyncHandler.emit``): доставщик не имеет права
#: писать в очередь, которую сам же и везёт.
LOGGER_NAME = "outbox"
LOGGER_NAME_FULL = "skillery.outbox"

#: Сколько конвертов ОДНОГО вида уходит одной отправкой.
BATCH_LIMIT = 50
#: Во сколько раз окно ЧТЕНИЯ шире батча. Смысл: батч режется по ``kind``, и
#: если один вид залип (например, логи без Bearer на незалогиненной машине), он
#: не имеет права занять всю голову очереди и уморить остальные голодом. Читать
#: шире почти бесплатно — файл всё равно читается целиком.
READ_OVERSCAN = 4
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


@dataclass
class _Failure:
    """Сбой ветки, из-за которого конверты остались в очереди."""

    status: int | None
    reason: str
    error_type: str
    pending: int
    kinds: list[str] = field(default_factory=list)


@dataclass
class _Outcome:
    """Исход одной ветки отправки."""

    ack: list[str] = field(default_factory=list)
    accepted: int = 0
    rejected: int = 0
    failure: _Failure | None = None
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


def _is_fatal(status: int | None) -> bool:
    """4xx, означающий «тело не примут В ПРИНЦИПЕ» (повтор бессмыслен)."""
    return (
        status is not None
        and 400 <= status < 500
        and status not in RETRYABLE_STATUSES
    )


def _kinds_of(envelopes: list[dict[str, Any]]) -> list[str]:
    return sorted({str(e.get("kind")) for e in envelopes})


def _ids_of(envelopes: list[dict[str, Any]]) -> list[str]:
    return [str(e.get("id")) for e in envelopes]


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


def _quarantine(
    envelopes: list[dict[str, Any]], *, status: int | None, reason: str,
    endpoint: str,
) -> _Outcome:
    """4xx на весь батч: удаляем с WARNING, иначе очередь встанет намертво."""
    ids = _ids_of(envelopes)
    _log.warning(
        "outbox: батч отвергнут (4xx) — карантин, повторять бессмысленно",
        extra={"context": {
            "status": status, "error": reason[:500], "removed": len(ids),
            "endpoint": endpoint, "kinds": _kinds_of(envelopes),
        }},
    )
    return _Outcome(ack=ids, rejected=len(ids), error=reason)


# ─────────────────────────── ветка ``/telemetry/batch`` ──────────────────────
async def _deliver_telemetry(
    client: Any, envelopes: list[dict[str, Any]], *, timeout: float
) -> _Outcome:
    """Конверты «всё, кроме аналитики» → ``POST /telemetry/batch``."""
    send = getattr(client, "send_telemetry_batch", None)
    if send is None:
        # Старый/урезанный клиент — НЕ теряем: доедет, когда появится метод.
        return _Outcome()

    try:
        response = await asyncio.wait_for(send(envelopes), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — доставка не валит вызывающего
        reason = str(exc) or repr(exc)
        status = _status_of(exc)
        if _is_fatal(status):
            return _quarantine(envelopes, status=status, reason=reason,
                               endpoint="/telemetry/batch")
        return _Outcome(
            error=reason or type(exc).__name__,
            failure=_Failure(
                status=status,
                reason=reason or type(exc).__name__,
                error_type=type(exc).__name__,
                pending=len(envelopes),
                kinds=_kinds_of(envelopes),
            ),
        )

    accepted, rejected = _split_response(response, envelopes)
    if rejected:
        # WARNING, а не ERROR: доставка исправна, невалиден конверт. Причина
        # обязана быть в логе — иначе потеря данных становится невидимой.
        rejected_ids = {str(r.get("id")) for r in rejected}
        _log.warning(
            "outbox: конверты отклонены бэкендом — удалены (повтор их не исправит)",
            extra={"context": {
                "rejected": len(rejected),
                "reasons": [str(r.get("reason") or "")[:200] for r in rejected[:5]],
                "kinds": sorted({str(e.get("kind")) for e in envelopes
                                 if str(e.get("id")) in rejected_ids}),
            }},
        )
    return _Outcome(
        ack=accepted + [str(r.get("id")) for r in rejected],
        accepted=len(accepted),
        rejected=len(rejected),
    )


# ─────────────────────────── ветка ``/events`` (аналитика) ───────────────────
async def _post_events(
    client: Any, dtos: list[dict[str, Any]], idem: str, *, timeout: float
) -> Any:
    return await asyncio.wait_for(
        client.ingest_events(dtos, idempotency_key=idem), timeout=timeout
    )


async def _retry_anonymous(
    anonymous_factory: Any, dtos: list[dict[str, Any]], idem: str, *,
    timeout: float,
) -> bool:
    """Повтор батча БЕЗ Bearer. ``True`` — уехало.

    ``anonymous_factory`` — ``() -> client | None``; ``None`` (и у фабрики, и
    вместо неё) означает «деградировать некуда» — токена и так не было, значит
    повтор дал бы тот же 401.
    """
    if anonymous_factory is None:
        return False
    try:
        anon = anonymous_factory()
    except Exception:  # noqa: BLE001 — не построился клиент → просто не ретраим
        return False
    if anon is None or getattr(anon, "ingest_events", None) is None:
        return False
    try:
        await _post_events(anon, dtos, idem, timeout=timeout)
        return True
    except asyncio.CancelledError:
        raise
    except BaseException:  # noqa: BLE001 — ретрай не удался → пусть полежит
        return False
    finally:
        close = getattr(anon, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 — закрытие не важнее доставки
                pass


async def _deliver_analytics(
    client: Any,
    envelopes: list[dict[str, Any]],
    *,
    timeout: float,
    anonymous_factory: Any = None,
) -> _Outcome:
    """Конверты ``kind="analytics_event"`` → ``POST /events`` (можно анонимно).

    Ответ ``/events`` — счётчик (``{"accepted": N}``), а не список id, поэтому
    ack идёт «всё или ничего»: успешный ответ подтверждает весь батч. Дубля это
    не создаёт — ``Idempotency-Key`` стабилен по содержимому батча, и бэкенд
    кеширует ответ.
    """
    from skillery_cli.core import analytics_sync

    if getattr(client, "ingest_events", None) is None:
        # Клиент не умеет /events — НЕ теряем, дождёмся умеющего.
        return _Outcome()

    dtos: list[dict[str, Any]] = []
    broken: list[str] = []
    for env in envelopes:
        item = analytics_sync.normalize_item(env.get("payload"))
        if item is None:
            broken.append(str(env.get("id")))
        else:
            dtos.append(item)
    if broken:
        # Конверт без ``event_type`` бэкенд не примет никогда — держать его в
        # очереди значит повторять один и тот же отказ вечно.
        _log.warning(
            "outbox: аналитические конверты без event_type — удалены",
            extra={"context": {"dropped": len(broken)}},
        )
    if not dtos:
        return _Outcome(ack=broken, rejected=len(broken))

    idem = analytics_sync.idempotency_key(dtos)
    try:
        await _post_events(client, dtos, idem, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — доставка не валит вызывающего
        reason = str(exc) or repr(exc)
        status = _status_of(exc)
        if status == 401 and await _retry_anonymous(
            anonymous_factory, dtos, idem, timeout=timeout
        ):
            # Протухшая сессия НЕ имеет права глушить телеметрию автономного CLI.
            return _Outcome(ack=_ids_of(envelopes), accepted=len(dtos),
                            rejected=len(broken))
        if _is_fatal(status):
            return _quarantine(envelopes, status=status, reason=reason,
                               endpoint="/events")
        return _Outcome(
            ack=broken,
            rejected=len(broken),
            error=reason or type(exc).__name__,
            failure=_Failure(
                status=status,
                reason=reason or type(exc).__name__,
                error_type=type(exc).__name__,
                pending=len(envelopes),
                kinds=[KIND_ANALYTICS_EVENT],
            ),
        )
    return _Outcome(ack=_ids_of(envelopes), accepted=len(dtos),
                    rejected=len(broken))


# ─────────────────────────── один проход доставки ───────────────────────────
def _plan(
    envelopes: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Окно чтения → (батч телеметрии, батч аналитики), каждый ≤ ``limit``."""
    analytics = [e for e in envelopes
                 if str(e.get("kind")) == KIND_ANALYTICS_EVENT]
    telemetry = [e for e in envelopes
                 if str(e.get("kind")) != KIND_ANALYTICS_EVENT]
    return telemetry[:limit], analytics[:limit]


async def flush_outbox(
    client: Any,
    *,
    limit: int = BATCH_LIMIT,
    timeout: float = FLUSH_TIMEOUT_SEC,
    force: bool = False,
    min_interval: float = MIN_FLUSH_INTERVAL_SEC,
    anonymous_factory: Any = None,
) -> OutboxFlushResult:
    """Один проход доставки: ``read_batch`` → отправка по ``kind`` → ``remove``.

    Возвращает :class:`OutboxFlushResult`; наверх бросает только
    ``CancelledError`` (см. :func:`flush_outbox_safe`, если и это нежелательно).

    ``anonymous_factory`` — ``() -> client | None`` для анонимного ретрая
    аналитики при 401 (см. докстринг модуля). ``None`` ⇒ ретрая нет.
    """
    global _LAST_FLUSH_AT

    from telemetrykit import outbox

    now = time.monotonic()
    if not is_due(force=force, min_interval=min_interval, now=now):
        return OutboxFlushResult(skipped=True)

    try:
        envelopes = outbox.read_batch(max(1, limit * READ_OVERSCAN))
    except Exception as exc:  # noqa: BLE001 — битый файл не валит демон
        _log.error("outbox: очередь не читается", extra={"context": {
            "error": str(exc) or repr(exc), "error_type": type(exc).__name__,
        }})
        return OutboxFlushResult(error=str(exc) or repr(exc))

    _LAST_FLUSH_AT = now
    if not envelopes:
        _record_success()  # пустая очередь = связь ни при чём, backoff снимаем
        return OutboxFlushResult()

    telemetry, analytics = _plan(envelopes, limit)
    read = len(telemetry) + len(analytics)

    outcomes: list[_Outcome] = []
    if telemetry:
        outcomes.append(
            await _deliver_telemetry(client, telemetry, timeout=timeout)
        )
    if analytics:
        outcomes.append(await _deliver_analytics(
            client, analytics, timeout=timeout,
            anonymous_factory=anonymous_factory,
        ))

    ack = [i for o in outcomes for i in o.ack]
    removed = 0
    if ack:
        try:
            removed = outbox.remove(ack)
        except Exception as exc:  # noqa: BLE001 — не смогли усечь → повтор/дубль
            _log.error("outbox: ack не записался", extra={"context": {
                "error": str(exc) or repr(exc), "ids": len(ack),
            }})

    accepted = sum(o.accepted for o in outcomes)
    rejected = sum(o.rejected for o in outcomes)
    failures = [o.failure for o in outcomes if o.failure is not None]

    # Backoff — про «связи нет», а не про «одна ветка капризничает». Пока хоть
    # что-то уезжает, тормозить всю очередь нельзя: иначе незалогиненная машина
    # (телеметрия без Bearer вечно 401) заодно душила бы анонимную аналитику.
    if removed > 0 or not failures:
        _record_success()
    else:
        _record_failure(now)

    for failure in failures:
        # ERROR, но БЕЗ exc_info: офлайн — штатное состояние ноутбука, а полный
        # трейс на каждый сбой сети делает лог нечитаемым.
        _log.error("outbox: батч не доставлен — повтор позже", extra={"context": {
            "status": failure.status,
            "error": failure.reason[:500],
            "error_type": failure.error_type,
            "pending": failure.pending,
            "kinds": failure.kinds,
            "retry_in": _BACKOFF_DELAY,
        }})

    if accepted:
        # Успех — INFO (не ERROR): на стандартном уровне лог не шумит, а при
        # разборе видно, сколько и чего реально уехало.
        _log.info("outbox: батч доставлен", extra={"context": {
            "read": read, "accepted": accepted,
            "rejected": rejected, "removed": removed,
        }})

    error = next((o.error for o in outcomes if o.error), None)
    return OutboxFlushResult(
        read=read,
        accepted=accepted,
        rejected=rejected,
        removed=removed,
        error=error,
    )


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
    "READ_OVERSCAN",
    "RETRYABLE_STATUSES",
    "OutboxFlushResult",
    "backoff_delay",
    "flush_outbox",
    "flush_outbox_safe",
    "is_due",
    "outbox_path",
    "reset_throttle",
]
