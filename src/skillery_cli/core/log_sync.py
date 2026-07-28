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

- :class:`LogSyncHandler` — кладёт запись в ОБЩИЙ outbox (не в сеть!) конвертом
  ``kind="log"``. Порог: WARNING на корне ``skillery`` (ошибки отовсюду) и INFO
  на ``skillery.install`` (аудит установки — в т.ч. foreground).
- Доставку делает ОДИН воркер — :mod:`skillery_cli.core.outbox_worker` в цикле
  демона. Здесь сети нет и не будет.

#1174: очередь ровно ОДНА
-------------------------
До #1174 у синка была СВОЯ очередь ``~/.skillery/logs/sync.queue.jsonl`` и своя
отправка на ``POST /cli-logs``, параллельно общему ``telemetrykit.outbox``, куда
навыки пишут ``kind="skill_run"``. Две очереди — это две разные гарантии
доставки и два места, где записи теряются. Теперь запись идёт в общий outbox, а
наследство перекладывается :func:`migrate_legacy_queue` при первом же старте
(идемпотентно, без потерь). Заводить вторую очередь — нельзя.

⚠️ Одиночный контракт ``HubClient.report_cli_log`` (``POST /cli-logs``) НЕ
тронут: на нём сидят старые CLI, и ломать их обратную совместимость эта задача
не имеет права.

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

import json
import logging
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

#: ``kind`` конверта общего outbox'а для лог-записи CLI.
KIND_LOG = "log"

#: Имя СТАРОЙ (до #1174) собственной очереди синка. Осталось только ради
#: миграции: файл может лежать на машинах пользователей с прежней версией.
LEGACY_QUEUE_FILENAME = "sync.queue.jsonl"

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


def legacy_queue_path() -> Path:
    """``~/.skillery/logs/sync.queue.jsonl`` — очередь ДО #1174 (только миграция)."""
    from skillery_cli.core.logging_setup import log_dir

    return log_dir() / LEGACY_QUEUE_FILENAME


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


# ─────────────────────────── запись в общий outbox ──────────────────────
def publish(item: dict[str, Any]) -> str | None:
    """Положить готовую лог-запись в ОБЩИЙ outbox конвертом ``kind="log"``.

    Никогда не бросает: кит недоступен / диск полон / outbox выключен ⇒ ``None``.
    """
    try:
        from telemetrykit import outbox

        return outbox.append(KIND_LOG, item)
    except Exception:  # noqa: BLE001 — телеметрия не валит команду
        return None


def migrate_legacy_queue(path: Path | None = None) -> int:
    """Перелить СТАРУЮ очередь синка в общий outbox. Возвращает число записей.

    Зачем: на машинах, обновившихся с версии до #1174, в
    ``~/.skillery/logs/sync.queue.jsonl`` могли остаться неотправленные записи —
    ровно те причины сбоев, ради которых логи и смотрят. Выбросить их вместе со
    старым механизмом было бы потерей пользовательских данных.

    Идемпотентно: после успешного переноса файл удаляется, повторный вызов
    возвращает 0. Битые строки пропускаются (одна не должна блокировать
    остальные). Ничего не бросает: миграция тише команды.
    """
    target = path
    try:
        target = target or legacy_queue_path()
        if not target.is_file():
            return 0
        raw = target.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 — нет доступа → просто не мигрируем
        return 0

    moved = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:  # noqa: BLE001 — обрыв записи/мусор
            continue
        if not isinstance(item, dict):
            continue
        if publish(item) is not None:
            moved += 1

    # Файл убираем ТОЛЬКО если всё, что в нём было пригодно, уехало в outbox.
    # Иначе (outbox выключен/недоступен) оставляем как есть — пусть попробует
    # следующий старт, чем потерять записи молча.
    if moved or not raw.strip():
        with suppress(OSError):
            target.unlink()
    if moved:
        # INFO в ``skillery.install`` (у него СВОЙ INFO-хендлер и он же синкается),
        # чтобы факт переноса был виден и в локальном логе, и в вебе.
        with suppress(Exception):
            logging.getLogger("skillery.install").info(
                "лог-синк: старая очередь перелита в общий outbox",
                extra={"context": {"moved": moved, "legacy": str(target)}},
            )
    return moved


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

#: Полное имя логгера воркера доставки (``outbox_worker.LOGGER_NAME_FULL``).
#: Держим строкой, а не импортом: :mod:`outbox_worker` — потребитель этого
#: модуля, обратный импорт завёл бы цикл ради одной константы. Сверено тестом
#: ``test_outbox_worker.py::test_worker_own_errors_do_not_feed_the_queue``.
_WORKER_LOGGER = "skillery.outbox"


class LogSyncHandler(logging.Handler):
    """Кладёт лог-запись в ОБЩИЙ outbox (``kind="log"``); сеть — дело воркера."""

    def __init__(
        self,
        *,
        level: int = logging.WARNING,
        limiter: _RateLimiter | None = None,
        sink: Any = None,
    ) -> None:
        super().__init__(level=level)
        self._limiter = limiter or _RateLimiter(
            limit=RATE_MAX_RECORDS, window=RATE_WINDOW_SEC
        )
        #: Точка подмены в тестах; прод-значение — :func:`publish`.
        self._sink = sink or publish

    def build_item(self, record: logging.LogRecord) -> dict[str, Any]:
        """LogRecord → payload конверта ``kind="log"``.

        Форма payload'а — ТА ЖЕ, что раньше уходила элементом ``POST /cli-logs``
        (level/logger/message/context/ts/stack): бэкенд разбирает её как и
        прежде, меняется только транспорт.
        """
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
            # #1174 анти-петля: доставщик общего outbox'а НЕ имеет права писать
            # в очередь, которую сам же и везёт. Иначе каждая ошибка доставки
            # рождает новый конверт → офлайн растит очередь сам от себя.
            if record.name == _WORKER_LOGGER or record.name.startswith(
                _WORKER_LOGGER + "."
            ):
                return
            # Одна запись = одна отправка. ``skillery.install`` в проде имеет
            # propagate=False, но полагаться на это нельзя: без метки ERROR из
            # него ушёл бы дважды (свой handler + корневой).
            if getattr(record, _SYNCED_FLAG, False):
                return
            setattr(record, _SYNCED_FLAG, True)
            if not self._limiter.allow():
                if self._limiter.should_report_drop():
                    self._sink({
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
            self._sink(self.build_item(record))
        except Exception:  # noqa: BLE001 — телеметрия не валит процесс
            return


def attach_log_sync(
    *,
    rate_max: int = RATE_MAX_RECORDS,
    rate_window: float = RATE_WINDOW_SEC,
    sink: Any = None,
) -> None:
    """Идемпотентно подключить синк к дереву логгеров ``skillery``.

    Два порога, потому что аудит установки живёт в отдельном логгере с
    ``propagate=False`` (см. ``logging_setup.install_logger``):

    - корень ``skillery`` — WARNING+ (ошибки демона/reconcile/heal/autostart);
    - ``skillery.install`` — INFO+ (аудит установки, включая foreground: шаги
      и итог; раньше foreground-install не синкался вовсе).

    Здесь же (#1174) перекладывается наследство: старая собственная очередь
    синка переливается в общий outbox. Делаем это ПОСЛЕ подключения handler'ов,
    чтобы сам факт миграции тоже уехал в веб.
    """
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
        logger.addHandler(LogSyncHandler(level=level, limiter=limiter, sink=sink))
    with suppress(Exception):  # миграция не имеет права ломать старт CLI
        migrate_legacy_queue()


def detach_log_sync() -> None:
    """Снять handler'ы синка (для тестов/диагностики)."""
    for name in ("skillery", "skillery.install"):
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            if isinstance(h, LogSyncHandler):
                logger.removeHandler(h)


__all__ = [
    "KIND_LOG",
    "LEGACY_QUEUE_FILENAME",
    "MAX_MESSAGE_LEN",
    "LogSyncHandler",
    "attach_log_sync",
    "detach_log_sync",
    "legacy_queue_path",
    "migrate_legacy_queue",
    "publish",
    "sanitize_context",
    "sanitize_text",
    "wire_level",
]
