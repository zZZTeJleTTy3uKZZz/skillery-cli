"""#1180: аналитические события CLI едут через ОБЩИЙ outbox (третьей очереди нет).

Зачем
-----
Владелец требует: очередь на машине ОДНА. До этой задачи их было три —
``telemetrykit.outbox`` (запуски навыков, ``kind="skill_run"``), собственная
очередь лог-синка (перелита в #1174) и ``~/.skillery/events.queue.json`` у
аналитики (``EventCollector``). Три очереди — это три разных гарантии доставки и
три места, где записи теряются молча.

Теперь продюсер (инструментация ``track_skill_event``, команда
``skillery event track``) просто дописывает конверт ``kind="analytics_event"`` в
общий outbox, а сеть — дело ОДНОГО воркера
(:mod:`skillery_cli.core.outbox_worker`) в цикле демона.

Почему аналитика уезжает НЕ через ``POST /telemetry/batch``
------------------------------------------------------------
``POST /events`` принимает АНОНИМНО (backend ``_optional_claims``), а
``POST /telemetry/batch`` требует Bearer. Аналитика ставится и на машине, где
пользователь не логинился вовсе (``install --path`` / ``--from-git``), и на
машине с протухшей сессией — именно на этих событиях стоит метрика активаций.
Отправь мы их в ``/telemetry/batch``, они бы вечно висели в очереди (401 —
повторяемый статус) и метрика бы просто исчезла.

Поэтому ОЧЕРЕДЬ общая (одна гарантия доставки, один файл, один воркер), а
ветка отправки у аналитики своя — внутри того же воркера. Бэкенд при этом не
меняется вообще: контракт ``/events`` тот же, что был у ``EventSender``.

Форма payload'а конверта — ДОСЛОВНО элемент ``events[]`` из ``POST /events``
(backend ``AnalyticsEventInputDTO``)::

    {"event_type", "occurred_at", "payload", "metadata",
     ["resource_type"], ["resource_id"]}

Инварианты
----------
- Ничего не бросает наверх: телеметрия не имеет права ронять команду.
- Дедуп/throttle (``EventGuard``) решает ДО публикации — это анти-спам
  продюсера, а не свойство очереди; после переезда он остался на месте.
- Старый файл ``events.queue.json`` перекладывается :func:`migrate_legacy_queue`
  при первом же старте CLI/демона: идемпотентно, без потерь, битые записи
  пропускаются, при недоступном outbox файл остаётся до следующего раза.
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: ``kind`` конверта общего outbox'а для аналитического события.
KIND_ANALYTICS_EVENT = "analytics_event"

#: Имя СТАРОЙ (до #1180) собственной очереди аналитики. Осталось только ради
#: миграции: файл лежит на машинах, обновившихся с прежней версии.
LEGACY_QUEUE_FILENAME = "events.queue.json"

#: Куда уезжает файл, который не разобрать как JSON-массив. Не удаляем молча:
#: потеря пользовательских данных обязана быть восстановимой.
LEGACY_CORRUPT_SUFFIX = ".corrupt.json"


def legacy_queue_path() -> Path:
    """``~/.skillery/events.queue.json`` — очередь ДО #1180 (только миграция).

    Каталог берём у ``config._default_config_dir`` — единого источника правды о
    доме CLI (ребренд + legacy-fallback + ``SKILLERY_CONFIG_DIR`` + профили).
    Своя копия этой логики уже приводила к тому, что демон писал мимо конфига.
    """
    from skillery_cli.config import _default_config_dir

    return _default_config_dir() / LEGACY_QUEUE_FILENAME


def build_item(
    event_type: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Собрать элемент ``events[]`` для ``POST /events`` (payload конверта)."""
    when = occurred_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    item: dict[str, Any] = {
        "event_type": str(event_type),
        "occurred_at": when.isoformat(),
        "payload": dict(payload or {}),
        "metadata": dict(metadata or {}),
    }
    if resource_type:
        item["resource_type"] = resource_type
    if resource_id:
        item["resource_id"] = resource_id
    return item


def normalize_item(raw: Any) -> dict[str, Any] | None:
    """Запись любого происхождения → элемент ``events[]`` или ``None``.

    Терпимо к наследству: в старой очереди ``resource_type``/``resource_id``
    могли лежать как ``null``, а ``payload``/``metadata`` — отсутствовать.
    Запись без ``event_type`` не событие — её пропускаем (одна битая строка не
    имеет права заблокировать остальные).
    """
    if not isinstance(raw, dict):
        return None
    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        return None
    item: dict[str, Any] = {
        "event_type": event_type,
        "occurred_at": str(raw.get("occurred_at") or datetime.now(UTC).isoformat()),
        "payload": dict(raw.get("payload") or {}),
        "metadata": dict(raw.get("metadata") or {}),
    }
    if raw.get("resource_type"):
        item["resource_type"] = str(raw["resource_type"])
    if raw.get("resource_id"):
        item["resource_id"] = str(raw["resource_id"])
    return item


def publish(item: dict[str, Any]) -> str | None:
    """Положить готовое событие в ОБЩИЙ outbox конвертом ``kind``-аналитики.

    Никогда не бросает: кит недоступен / диск полон / outbox выключен ⇒ ``None``.
    """
    try:
        from telemetrykit import outbox

        return outbox.append(KIND_ANALYTICS_EVENT, item)
    except Exception:  # noqa: BLE001 — телеметрия не валит команду
        return None


def track(
    event_type: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Собрать событие и положить в общий outbox. ``None``, если не записалось."""
    item = build_item(
        event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        payload=payload,
        metadata=metadata,
        occurred_at=occurred_at,
    )
    return item if publish(item) is not None else None


# ─────────────────────────── чтение очереди (диагностика) ───────────────────
#: Сколько конвертов читаем при инспекции очереди. Файл ротируется китом на
#: 5 МБ, так что верхняя граница здесь — предохранитель, а не реальный объём.
_INSPECT_LIMIT = 10_000


def _analytics_envelopes(limit: int = _INSPECT_LIMIT) -> list[dict[str, Any]]:
    try:
        from telemetrykit import outbox

        return [
            e for e in outbox.read_batch(limit)
            if str(e.get("kind")) == KIND_ANALYTICS_EVENT
        ]
    except Exception:  # noqa: BLE001 — инспекция очереди не валит команду
        return []


def pending(limit: int = _INSPECT_LIMIT) -> list[dict[str, Any]]:
    """Аналитические события, ждущие отправки (payload'ы конвертов)."""
    out: list[dict[str, Any]] = []
    for env in _analytics_envelopes(limit):
        item = normalize_item(env.get("payload"))
        if item is not None:
            out.append(item)
    return out


def pending_count() -> int:
    """Сколько аналитических событий ждёт отправки."""
    return len(_analytics_envelopes())


def clear_pending() -> int:
    """Выбросить аналитические конверты из общего outbox. Возвращает число.

    Только ``kind`` аналитики: ``skillery event queue --clear`` не имеет права
    трогать чужие конверты (логи, запуски навыков) в общей очереди.
    """
    ids = [str(e.get("id")) for e in _analytics_envelopes()]
    if not ids:
        return 0
    try:
        from telemetrykit import outbox

        return outbox.remove(ids)
    except Exception:  # noqa: BLE001
        return 0


def outbox_path() -> Path | None:
    """Путь к ОБЩЕЙ очереди (для ``daemon status`` / ``event queue``)."""
    try:
        from telemetrykit import outbox

        return outbox.path()
    except Exception:  # noqa: BLE001
        return None


def idempotency_key(batch: list[dict[str, Any]]) -> str:
    """Стабильный ``Idempotency-Key`` от содержимого батча.

    Одинаковый батч → одинаковый key, backend кеширует ответ, поэтому повтор
    после обрыва между отправкой и ack не создаёт дубликатов. Форма ключа
    сохранена от ``EventSender`` — бэкенд по нему уже кеширует.
    """
    canon = json.dumps(batch, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return f"sh-cli-{digest[:24]}"


# ─────────────────────────── миграция наследства ────────────────────────────
def migrate_legacy_queue(path: Path | None = None) -> int:
    """Перелить СТАРУЮ очередь аналитики в общий outbox. Возвращает число записей.

    Зачем: на машинах, обновившихся с версии до #1180, в
    ``~/.skillery/events.queue.json`` остались неотправленные события — ровно те
    установки/включения, на которых стоит метрика адопции. Выбросить их вместе
    со старым механизмом было бы потерей пользовательских данных.

    Идемпотентно: после успешного переноса файл удаляется, повторный вызов
    возвращает 0. Битые записи пропускаются (одна не должна блокировать
    остальные), нечитаемый как JSON-массив файл уводится в ``*.corrupt.json``
    (иначе миграция пыталась бы его разобрать при каждом старте вечно).
    Ничего не бросает: миграция тише команды.
    """
    target = path
    try:
        target = target or legacy_queue_path()
        if not target.is_file():
            return 0
        raw = target.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 — нет доступа → просто не мигрируем
        return 0

    if not raw.strip():
        with suppress(OSError):
            target.unlink()
        return 0

    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — файл не JSON вовсе
        data = None
    if not isinstance(data, list):
        # Не молча удаляем: содержимое может понадобиться для разбора, а
        # оставленный на месте файл заставлял бы миграцию буксовать вечно.
        with suppress(OSError):
            target.replace(target.with_suffix(LEGACY_CORRUPT_SUFFIX))
        return 0

    items = [i for i in (normalize_item(d) for d in data) if i is not None]
    moved = sum(1 for item in items if publish(item) is not None)

    # Файл убираем ТОЛЬКО если ВСЁ пригодное уехало. Иначе (outbox выключен или
    # недоступен) оставляем как есть — пусть попробует следующий старт, чем
    # потерять записи молча.
    if moved == len(items):
        with suppress(OSError):
            target.unlink()
    if moved:
        # INFO в ``skillery.install`` (у него СВОЙ INFO-хендлер и он синкается),
        # чтобы факт переноса был виден и в локальном логе, и в вебе.
        with suppress(Exception):
            logging.getLogger("skillery.install").info(
                "аналитика: старая очередь перелита в общий outbox",
                extra={"context": {"moved": moved, "legacy": str(target)}},
            )
    return moved


__all__ = [
    "KIND_ANALYTICS_EVENT",
    "LEGACY_CORRUPT_SUFFIX",
    "LEGACY_QUEUE_FILENAME",
    "build_item",
    "clear_pending",
    "idempotency_key",
    "legacy_queue_path",
    "migrate_legacy_queue",
    "normalize_item",
    "outbox_path",
    "pending",
    "pending_count",
    "publish",
    "track",
]
