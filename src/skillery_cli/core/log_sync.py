"""C3 (#1099): автосинк логов CLI на бэкенд — полный, буферизованный, тихий.

Зачем
-----
Логи CLI обязаны АВТОСИНХРОНИЗИРОВАТЬСЯ с хабом, чтобы проблемы устройств
разбирались из веба (``/logs``): с уровнем (C1), инициатором (C2: ``cli`` |
``web-queue`` | ``daemon-auto``) и ПРИЧИНОЙ. Раньше синк был фрагментарным —
ровно три вызова ``report_cli_log`` (login + успешные install/uninstall из
веб-очереди), всегда ``level=info`` и только на УСПЕХ. Провалы, ради которых
логи и смотрят, до бэка не доезжали вовсе; foreground-install не синкался.

Как устроено
------------
Синк подключается НЕ россыпью вызовов по коду, а одним handler'ом поверх уже
существующего дерева логгеров ``skillery`` (C1) — поэтому автоматически
покрывает КАЖДУЮ точку, которая уже пишет в ``cli.log``/``daemon.log``:
провал install, провал device-task, ошибку демона/reconcile, autostart/heal.

- :class:`LogSyncHandler` — кладёт запись в дисковую очередь (не в сеть!).
  Порог: WARNING на корне ``skillery`` (ошибки отовсюду) и INFO на
  ``skillery.install`` (аудит установки — в т.ч. foreground).
- :class:`LogSyncQueue` — ``~/.skillery/logs/sync.queue.jsonl``: append-only
  JSONL (append = O(1), в отличие от event-очереди с перезаписью всего файла),
  ОГРАНИЧЕННАЯ по размеру и числу записей. Офлайн ⇒ ничего не теряем.
- :func:`flush_log_sync` — best-effort досылка батчем: жёсткий таймаут (синк
  НЕ имеет права держать install/демон), при сбое запись возвращается в
  очередь и уедет следующим циклом демона.

Инварианты
----------
- Уровни/инициатор C1/C2 НЕ меняются: на провод едет backend-алфавит
  (``debug|info|warning|error``), исходный уровень сохраняется в
  ``context.cli_level`` (TRACE/ACCESS схлопываются в ``debug``).
- Секреты маскируются ЕДИНЫМ сканером (``core.secret_scan`` → skillgate),
  «секретные» ключи контекста выбрасываются, длинные строки обрезаются под
  лимиты бэкенда.
- Ничего не бросает наверх: любая ошибка синка тише самой команды.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

# Лимиты строк совпадают с backend ``ClientLogItem`` (interface/http/schemas/
# audit.py) — обрезаем на клиенте, чтобы 422 не убивал весь батч.
MAX_MESSAGE_LEN = 4000
MAX_LOGGER_LEN = 64
MAX_STACK_LEN = 16_000
MAX_CONTEXT_VALUE_LEN = 500
MAX_CONTEXT_KEYS = 40

QUEUE_FILENAME = "sync.queue.jsonl"
#: Потолок дискового буфера. Переполнение выкидывает САМЫЕ СТАРЫЕ записи —
#: свежая причина сбоя важнее позавчерашней.
MAX_QUEUE_RECORDS = 500
MAX_QUEUE_BYTES = 512_000

#: Сколько записей уходит одним POST /cli-logs (backend принимает до 200).
BATCH_LIMIT = 50
#: Жёсткий таймаут одной отправки — синк не держит install/демон.
FLUSH_TIMEOUT_SEC = 5.0
#: Минимальная пауза между сетевыми попытками в ОДНОМ процессе (анти-спам).
#: Цикл демона пробивает её через ``force=True``.
MIN_FLUSH_INTERVAL_SEC = 20.0

#: Анти-спам на ЗАПИСЬ: не больше N записей за окно (шторм ошибок в цикле
#: демона не должен вымыть буфер и завалить бэк).
RATE_MAX_RECORDS = 100
RATE_WINDOW_SEC = 60.0

# Ключи контекста, значение которых не отправляем НИКОГДА (даже маскированным).
_SECRET_KEY_MARKERS = (
    "token", "password", "passwd", "secret", "authorization", "auth_header",
    "api_key", "apikey", "credential", "private_key", "cookie",
)

# CLI-уровни (C1) → алфавит backend ``LogLevel``. TRACE/ACCESS ниже INFO ⇒
# схлопываются в ``debug``; исходное имя уровня сохраняется в context.cli_level.
_WIRE_LEVEL = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "ACCESS": "debug",
    "INFO": "info",
    "WARNING": "warning",
    "WARN": "warning",
    "ERROR": "error",
    "CRITICAL": "error",
    "FATAL": "error",
}


def wire_level(name: str | None) -> str:
    """Имя CLI-уровня → уровень бэкенда (``debug|info|warning|error``)."""
    return _WIRE_LEVEL.get((name or "").strip().upper(), "info")


def default_queue_path() -> Path:
    """``~/.skillery/logs/sync.queue.jsonl`` (рядом с cli.log/daemon.log)."""
    from skillery_cli.core.logging_setup import log_dir

    return log_dir() / QUEUE_FILENAME


# ─────────────────────────── маскирование ───────────────────────────────
def _mask_secrets(text: str) -> str:
    """Замаскировать секреты в тексте ЕДИНЫМ сканером проекта.

    Маскируем только те строки, которые пометил сканер (skillgate через
    ``core.secret_scan``) — иначе агрессивный ``mask_line`` съел бы обычные
    slug'и/пути/версии и лог перестал бы быть диагностичным.
    """
    if not text:
        return text
    try:
        from skillery_cli.core import secret_scan

        findings = secret_scan.scan_text("cli.log", text)
        if not findings:
            return text
        lines = text.splitlines()
        for f in findings:
            idx = int(getattr(f, "line", 0)) - 1
            if 0 <= idx < len(lines):
                lines[idx] = secret_scan.mask_line(lines[idx])
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — сканер не обязан быть доступен
        return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def sanitize_text(text: str, *, limit: int = MAX_MESSAGE_LEN) -> str:
    """Маскирование секретов + обрезка под лимит бэкенда."""
    return _truncate(_mask_secrets(str(text or "")), limit)


def _is_secret_key(key: str) -> bool:
    k = key.strip().lower()
    return any(marker in k for marker in _SECRET_KEY_MARKERS)


def sanitize_context(context: Any) -> dict[str, Any]:
    """Контекст записи → безопасный JSON-совместимый dict.

    Секретные ключи выбрасываются целиком, строковые значения маскируются и
    обрезаются, число ключей ограничено (контекст — не транспорт для дампов).
    """
    if not isinstance(context, dict):
        return {}
    out: dict[str, Any] = {}
    for raw_key, value in list(context.items())[:MAX_CONTEXT_KEYS]:
        key = str(raw_key)
        if _is_secret_key(key):
            out[key] = "***"
            continue
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = sanitize_text(value, limit=MAX_CONTEXT_VALUE_LEN)
        else:
            out[key] = sanitize_text(repr(value), limit=MAX_CONTEXT_VALUE_LEN)
    return out


def _device_context() -> dict[str, Any]:
    """Привязка записи к машине: без неё лог из веба не разобрать."""
    ctx: dict[str, Any] = {}
    try:
        from skillery_cli import __version__ as cli_version
        from skillery_cli.core.identity import device_uid

        ctx["client_device_id"] = device_uid()
        ctx["cli_version"] = cli_version
    except Exception:  # noqa: BLE001 — идентичность не критична для записи
        pass
    try:
        import platform as _pf

        ctx["platform"] = (_pf.system() or "unknown").strip().lower() or "unknown"
    except Exception:  # noqa: BLE001
        pass
    return ctx


# ─────────────────────────── дисковая очередь ───────────────────────────
class LogSyncQueue:
    """Ограниченная append-only JSONL-очередь логов, ждущих отправки.

    Почему не :class:`~skillery_cli.daemon.event_collector.EventCollector`:
    у него другая форма записи (analytics-event) и другой эндпоинт, а главное —
    он перезаписывает ВЕСЬ файл на каждый append (O(n)). Здесь append зовётся
    из logging-handler'а на каждой ошибке, поэтому нужен O(1)-дозапись JSONL.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_records: int = MAX_QUEUE_RECORDS,
        max_bytes: int = MAX_QUEUE_BYTES,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._max_records = max(1, int(max_records))
        self._max_bytes = max(1024, int(max_bytes))
        # Счётчик записей в файле — чтобы держать лимит ТОЧНО, но платить
        # полным чтением только когда очередь реально переполнилась.
        self._count: int | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = default_queue_path()
        return self._path

    # --- запись -------------------------------------------------------
    def append(self, item: dict[str, Any]) -> bool:
        """Дописать запись. Никогда не бросает — синк тише команды."""
        try:
            line = json.dumps(item, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 — неупаковываемая запись не нужна
            return False
        try:
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._count is None:
                self._count = len(self.read_all())
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._count += 1
            if (
                self._count > self._max_records
                or path.stat().st_size > self._max_bytes
            ):
                self._write_bounded(self.read_all())
            return True
        except Exception:  # noqa: BLE001 — RO-FS/гонка не валят процесс
            return False

    def _write_bounded(self, items: list[dict[str, Any]]) -> None:
        """Перезаписать очередь, оставив САМЫЕ СВЕЖИЕ записи в пределах лимитов."""
        kept: list[str] = []
        total = 0
        for item in reversed(items[-self._max_records:]):
            try:
                line = json.dumps(item, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                continue
            size = len(line.encode("utf-8")) + 1
            if kept and total + size > self._max_bytes:
                break
            kept.append(line)
            total += size
        kept.reverse()
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            ("\n".join(kept) + "\n") if kept else "", encoding="utf-8"
        )
        os.replace(tmp, path)
        self._count = len(kept)

    # --- чтение -------------------------------------------------------
    def read_all(self) -> list[dict[str, Any]]:
        """Все записи (битые строки пропускаем — очередь не должна «залипать»)."""
        try:
            path = self.path
            if not path.is_file():
                return []
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return []
        items: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:  # noqa: BLE001 — обрыв записи/мусор
                continue
            if isinstance(data, dict):
                items.append(data)
        return items

    def size(self) -> int:
        return len(self.read_all())

    def drain(self, limit: int = BATCH_LIMIT) -> list[dict[str, Any]]:
        """Снять до ``limit`` записей с ГОЛОВЫ (старые первыми), остаток оставить."""
        if limit <= 0:
            return []
        items = self.read_all()
        if not items:
            return []
        head, tail = items[:limit], items[limit:]
        try:
            self._write_bounded(tail)
        except Exception:  # noqa: BLE001 — не смогли усечь → отдадим копию,
            return head  # дубль в вебе лучше потери причины сбоя
        return head

    def requeue(self, items: list[dict[str, Any]]) -> None:
        """Вернуть неотправленные записи в голову (офлайн → следующий цикл)."""
        if not items:
            return
        with suppress(Exception):  # RO-FS не повод валить вызывающего
            self._write_bounded(list(items) + self.read_all())

    def clear(self) -> None:
        with suppress(Exception):
            self._write_bounded([])


# ─────────────────────────── rate-limit ─────────────────────────────────
class _RateLimiter:
    """Скользящее окно на ЗАПИСЬ: не больше ``limit`` записей за ``window`` сек."""

    def __init__(self, *, limit: int, window: float) -> None:
        self._limit = max(1, int(limit))
        self._window = max(0.001, float(window))
        self._started = 0.0
        self._count = 0
        self._reported = False

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._started >= self._window:
            self._started = now
            self._count = 0
            self._reported = False
        self._count += 1
        return self._count <= self._limit

    def should_report_drop(self) -> bool:
        """True ровно один раз за окно — чтобы факт отбрасывания был виден."""
        if self._reported:
            return False
        self._reported = True
        return True


# ─────────────────────────── logging handler ────────────────────────────
#: Метка на LogRecord: «эта запись уже поставлена в очередь синка».
_SYNCED_FLAG = "_skillery_log_synced"


class LogSyncHandler(logging.Handler):
    """Кладёт лог-запись в :class:`LogSyncQueue` (сеть — отдельно, во flush)."""

    def __init__(
        self,
        queue: LogSyncQueue,
        *,
        level: int = logging.WARNING,
        limiter: _RateLimiter | None = None,
    ) -> None:
        super().__init__(level=level)
        self._queue = queue
        self._limiter = limiter or _RateLimiter(
            limit=RATE_MAX_RECORDS, window=RATE_WINDOW_SEC
        )

    def build_item(self, record: logging.LogRecord) -> dict[str, Any]:
        """LogRecord → payload элемента ``POST /cli-logs``."""
        from datetime import UTC, datetime

        context = sanitize_context(getattr(record, "context", None))
        context["cli_level"] = record.levelname
        context.update(_device_context())
        item: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": wire_level(record.levelname),
            "logger": str(record.name)[:MAX_LOGGER_LEN],
            "message": sanitize_text(record.getMessage()),
            "context": context,
        }
        if record.exc_info:
            item["stack"] = sanitize_text(
                self.format_exception(record), limit=MAX_STACK_LEN
            )
        return item

    @staticmethod
    def format_exception(record: logging.LogRecord) -> str:
        import traceback

        return "".join(traceback.format_exception(*record.exc_info))  # type: ignore[misc]

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            # Одна запись = одна отправка. ``skillery.install`` в проде имеет
            # propagate=False, но полагаться на это нельзя: без метки ERROR из
            # него ушёл бы дважды (свой handler + корневой).
            if getattr(record, _SYNCED_FLAG, False):
                return
            setattr(record, _SYNCED_FLAG, True)
            if not self._limiter.allow():
                if self._limiter.should_report_drop():
                    self._queue.append({
                        "ts": None,
                        "level": "warning",
                        "logger": "skillery.log_sync",
                        "message": (
                            "лог-синк: сработало ограничение частоты — часть "
                            "записей не отправлена (полный журнал на устройстве: "
                            "skillery logs)"
                        ),
                        "context": _device_context(),
                    })
                return
            self._queue.append(self.build_item(record))
        except Exception:  # noqa: BLE001 — телеметрия не валит процесс
            return


def attach_log_sync(
    *,
    queue: LogSyncQueue | None = None,
    rate_max: int = RATE_MAX_RECORDS,
    rate_window: float = RATE_WINDOW_SEC,
) -> LogSyncQueue:
    """Идемпотентно подключить синк к дереву логгеров ``skillery``.

    Два порога, потому что аудит установки живёт в отдельном логгере с
    ``propagate=False`` (см. ``logging_setup.install_logger``):

    - корень ``skillery`` — WARNING+ (ошибки демона/reconcile/heal/autostart);
    - ``skillery.install`` — INFO+ (аудит установки, включая foreground: шаги
      и итог; раньше foreground-install не синкался вовсе).
    """
    q = queue or LogSyncQueue()
    limiter = _RateLimiter(limit=rate_max, window=rate_window)
    for name, level in (("skillery", logging.WARNING),
                        ("skillery.install", logging.INFO)):
        logger = logging.getLogger(name)
        if any(isinstance(h, LogSyncHandler) for h in logger.handlers):
            continue
        # Логгер обязан пропускать записи до handler'а: корень по умолчанию
        # молчит (уровень ERROR из конфига) — на нём порог хендлера и решает.
        if logger.level > level or logger.level == logging.NOTSET:
            logger.setLevel(min(logger.level or level, level))
        logger.addHandler(LogSyncHandler(q, level=level, limiter=limiter))
    return q


def detach_log_sync() -> None:
    """Снять handler'ы синка (для тестов/диагностики)."""
    for name in ("skillery", "skillery.install"):
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            if isinstance(h, LogSyncHandler):
                logger.removeHandler(h)


# ─────────────────────────── отправка ───────────────────────────────────
_LAST_FLUSH_AT = 0.0


def reset_flush_throttle() -> None:
    """Сбросить троттл отправки (тесты/принудительная досылка)."""
    global _LAST_FLUSH_AT
    _LAST_FLUSH_AT = 0.0


async def flush_log_sync(
    client: Any,
    *,
    queue: LogSyncQueue | None = None,
    limit: int = BATCH_LIMIT,
    timeout: float = FLUSH_TIMEOUT_SEC,
    force: bool = False,
    min_interval: float = MIN_FLUSH_INTERVAL_SEC,
) -> int:
    """Best-effort досылка буфера на ``POST /cli-logs``. Возвращает число ушедших.

    Гарантии:
    - НЕ блокирует вызывающего дольше ``timeout`` (жёсткий ``wait_for``);
    - НЕ теряет записи: сбой/таймаут/офлайн ⇒ ``requeue`` (уедет следующим
      циклом демона);
    - НЕ спамит: в одном процессе не чаще ``min_interval`` (кроме ``force``,
      которым пользуется цикл демона), не больше ``limit`` записей за запрос;
    - НЕ бросает: любая ошибка синка тише самой команды.
    """
    global _LAST_FLUSH_AT
    now = time.monotonic()
    if not force and _LAST_FLUSH_AT and (now - _LAST_FLUSH_AT) < min_interval:
        return 0
    q = queue or LogSyncQueue()
    try:
        items = q.drain(limit)
    except Exception:  # noqa: BLE001
        return 0
    _LAST_FLUSH_AT = now
    if not items:
        return 0
    send = getattr(client, "report_cli_logs", None)
    if send is None:  # старый/урезанный клиент — не теряем, ждём следующего
        q.requeue(items)
        return 0
    try:
        await asyncio.wait_for(send(items), timeout=timeout)
    except asyncio.CancelledError:
        q.requeue(items)
        raise
    except Exception:  # noqa: BLE001 — офлайн/таймаут/4xx → досылаем позже
        q.requeue(items)
        return 0
    return len(items)


async def flush_log_sync_safe(client: Any, **kwargs: Any) -> int:
    """:func:`flush_log_sync`, который не бросает вообще ничего (кроме отмены)."""
    try:
        return await flush_log_sync(client, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "BATCH_LIMIT",
    "MAX_MESSAGE_LEN",
    "LogSyncHandler",
    "LogSyncQueue",
    "attach_log_sync",
    "default_queue_path",
    "detach_log_sync",
    "flush_log_sync",
    "flush_log_sync_safe",
    "reset_flush_throttle",
    "sanitize_context",
    "sanitize_text",
    "wire_level",
]
