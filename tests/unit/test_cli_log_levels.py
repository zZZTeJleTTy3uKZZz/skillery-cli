"""C1: уровни логирования CLI — ACCESS, env-override, инвариант error/debug.

Владелец: логировать ВСЁ по уровням; стандарт = ТОЛЬКО ошибки (ERROR),
debug/verbose = весь трейс. Здесь закреплено:
- уровень ACCESS живёт между DEBUG и INFO (факт опроса/доступа);
- env ``SKILLERY_LOG_LEVEL`` переопределяет cfg-уровень (рантайм-переключатель);
- на уровне error лог НЕ содержит debug-записей; на debug — содержит.
"""
from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path

import pytest

from skillery_cli.core import logging_setup as ls

_LOGGER_NAMES = (
    "skillery", "skillery.install", "skillery.daemon", "skillery.reconcile",
)


@pytest.fixture(autouse=True)
def _reset_loggers():
    """Снять file-хендлеры дерева skillery между тестами (tmp-файлы разные)."""

    def _clear() -> None:
        for name in _LOGGER_NAMES:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                with suppress(Exception):
                    h.close()

    _clear()
    ls._configured = False
    yield
    _clear()
    ls._configured = False


def _records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def test_access_level_sits_between_debug_and_info() -> None:
    assert logging.DEBUG < ls.ACCESS < logging.INFO
    assert logging.getLevelName(ls.ACCESS) == "ACCESS"
    assert ls.normalize_level("access") == "ACCESS"


def test_access_helper_writes_at_access_and_drops_lower(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("access", filename="cli.log")
    log = ls.get_logger("reconcile")

    ls.access(log, "очередь опрошена", extra={"context": {"size": 3}})
    log.debug("детальный трейс")  # ниже ACCESS — не должно попасть

    recs = _records(tmp_path / "cli.log")
    assert any(
        r["message"] == "очередь опрошена" and r["level"] == "ACCESS"
        for r in recs
    )
    assert not any(r["message"] == "детальный трейс" for r in recs)


def test_error_level_drops_debug_but_keeps_error(tmp_path, monkeypatch) -> None:
    """Инвариант стандартного режима: при error лог НЕ пухнет debug-записями."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("error", filename="cli.log")
    log = ls.get_logger("daemon")

    log.debug("трейс такта")
    log.error("провал такта")

    recs = _records(tmp_path / "cli.log")
    assert not any(r["message"] == "трейс такта" for r in recs)
    assert any(r["message"] == "провал такта" and r["level"] == "ERROR" for r in recs)


def test_debug_level_includes_debug_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("debug", filename="cli.log")
    log = ls.get_logger("daemon")

    log.debug("трейс такта")

    recs = _records(tmp_path / "cli.log")
    assert any(
        r["message"] == "трейс такта" and r["level"] == "DEBUG" for r in recs
    )


def test_env_overrides_cfg_level(tmp_path, monkeypatch) -> None:
    """env SKILLERY_LOG_LEVEL=debug побеждает cfg=error (рантайм-переключатель)."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.setenv("SKILLERY_LOG_LEVEL", "debug")
    # cfg говорит error, но env поднимает до debug.
    ls.configure_logging("error", filename="cli.log")
    ls.get_logger("daemon").debug("трейс из env")

    recs = _records(tmp_path / "cli.log")
    assert any(r["message"] == "трейс из env" for r in recs)


def test_effective_level_prefers_env(monkeypatch) -> None:
    monkeypatch.setenv("SKILLERY_LOG_LEVEL", "trace")
    assert ls.effective_level("error") == "TRACE"
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    assert ls.effective_level("error") == "ERROR"
    assert ls.effective_level(None) == "ERROR"
