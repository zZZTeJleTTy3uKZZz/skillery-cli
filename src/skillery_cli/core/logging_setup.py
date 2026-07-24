"""Структурные логи CLI в ``~/.skillery/logs/`` с уровнями (как в бэке/вебе).

Раньше CLI писал только ``last-error.log`` и ``upgrade.log`` в КОРЕНЬ
``~/.skillery`` — без уровней и без общего журнала действий. Здесь единый
инициализатор: ротируемый файл ``logs/cli.log`` (+ ``logs/daemon.log`` для
демона), уровень из конфига (по умолчанию ERROR; включаемый DEBUG/TRACE).

Формат — по строке JSON на запись (ts/level/logger/message + extra), чтобы и
человеку читалось, и парсилось при отправке в бэкенд (#1024).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path

# TRACE — уровень ниже DEBUG для «полного трейса» по запросу пользователя.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_LEVELS = {"TRACE": TRACE, "DEBUG": logging.DEBUG, "INFO": logging.INFO,
           "WARNING": logging.WARNING, "ERROR": logging.ERROR}

_ROOT_NAME = "skillery"
_configured = False


def log_dir() -> Path:
    from skillery_cli import _branding

    d = Path.home() / _branding.HOME_DIR_NAME / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_level(value: str | None) -> str:
    v = (value or "").strip().upper()
    return v if v in _LEVELS else "ERROR"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # extra-поля (context=...) — если переданы через logger.x(..., extra={...})
        ctx = getattr(record, "context", None)
        if isinstance(ctx, dict):
            base["context"] = ctx
        if record.exc_info:
            base["stack"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


def configure_logging(level: str | None = None, *, filename: str = "cli.log") -> None:
    """Идемпотентно настроить логгер ``skillery`` на ротируемый файл в logs/.

    ``level`` — строка уровня (из конфига); None ⇒ ERROR. ``filename`` —
    ``cli.log`` для команд, ``daemon.log`` для демона.
    """
    global _configured
    logger = logging.getLogger(_ROOT_NAME)
    lvl = _LEVELS[normalize_level(level)]
    logger.setLevel(lvl)
    # Не даём всплывать в root (чтобы не сыпать в stderr пользователю).
    logger.propagate = False

    target = log_dir() / filename
    # Не плодим хендлеры при повторном вызове на тот же файл.
    for h in logger.handlers:
        if getattr(h, "_skillery_target", None) == str(target):
            h.setLevel(lvl)
            _configured = True
            return
    try:
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — логи не должны валить CLI
        return
    handler._skillery_target = str(target)  # type: ignore[attr-defined]
    handler.setFormatter(_JsonFormatter())
    handler.setLevel(lvl)
    logger.addHandler(handler)
    _configured = True


def get_logger(name: str = _ROOT_NAME) -> logging.Logger:
    """Логгер приложения. Имя дочерних — ``skillery.<sub>`` (cli/daemon/install)."""
    if name == _ROOT_NAME:
        return logging.getLogger(_ROOT_NAME)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def install_logger(filename: str = "cli.log") -> logging.Logger:
    """Аудит-логгер установки навыка — ВСЕГДА пишет INFO в ``logs/<filename>``.

    Обычный уровень cli.log/daemon.log — ERROR (тихо), поэтому шаги установки в
    него не попадали, и «тихий пропуск» установки CLI-пакета (SK-2) невозможно было
    диагностировать без ручного разбора (cli.log/daemon.log были 0 байт). Этот
    логгер несёт СВОЙ файл-хендлер на уровне INFO и ``propagate=False``: аудит
    установки пишется всегда и в тот же файл, но НЕ зависит от общего уровня и не
    делает лог демона болтливым (прочие логгеры остаются на своём уровне).

    ``filename`` — ``cli.log`` (foreground) или ``daemon.log`` (фоновый демон:
    stdout→DEVNULL, поэтому только файл). Идемпотентно (хендлер на файл — один).
    """
    lg = logging.getLogger(f"{_ROOT_NAME}.install")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    target = log_dir() / filename
    key = str(target)
    for h in lg.handlers:
        if getattr(h, "_skillery_target", None) == key:
            return lg
    try:
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — логи не должны валить install
        return lg
    handler._skillery_target = key  # type: ignore[attr-defined]
    handler.setFormatter(_JsonFormatter())
    handler.setLevel(logging.INFO)
    lg.addHandler(handler)
    return lg
