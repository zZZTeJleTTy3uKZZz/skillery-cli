"""C1: демон-цикл наполняет трейс (DEBUG на такт) и логирует ошибки (ERROR).

Раньше ``DaemonRunner.cycle_once``/``run_forever`` были тихими: на debug ничего
не появлялось, а провал reconcile глотался ``suppress`` без следа. Здесь:
- на каждый такт при debug пишется DEBUG-запись (sent/accepted/requeued);
- провал reconcile → ERROR (но цикл НЕ падает — reconcile best-effort).
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

import pytest

from skillery_cli.core import logging_setup as ls
from skillery_cli.daemon.daemon_runner import DaemonRunner
from skillery_cli.daemon.event_sender import SendResult

_LOGGER_NAMES = ("skillery", "skillery.daemon", "skillery.reconcile")


@pytest.fixture(autouse=True)
def _reset_loggers():
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


class _Sender:
    def __init__(self, result: SendResult) -> None:
        self._result = result

    async def send_once(self) -> SendResult:
        return self._result


def _runner(tmp_path: Path, sender, **kw) -> DaemonRunner:
    return DaemonRunner(
        sender,  # type: ignore[arg-type]
        interval_seconds=60,
        pid_path=tmp_path / "d.pid",
        state_path=tmp_path / "d.state.json",
        **kw,
    )


def test_cycle_once_writes_debug_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("debug", filename="daemon.log")

    sender = _Sender(SendResult(sent=2, accepted=2, skipped=0, requeued=0, last_error=None))
    runner = _runner(tmp_path, sender)
    asyncio.run(runner.cycle_once())

    recs = _records(tmp_path / "daemon.log")
    debug = [r for r in recs if r["level"] == "DEBUG"]
    assert debug, "на debug каждый такт обязан оставлять запись"
    assert any(r["context"].get("sent") == 2 for r in debug)


def test_cycle_once_is_silent_on_error_level(tmp_path, monkeypatch) -> None:
    """Стандартный режим (error): пустой такт НЕ пишет debug-шум."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("error", filename="daemon.log")

    sender = _Sender(SendResult(sent=0, accepted=0, skipped=0, requeued=0, last_error=None))
    runner = _runner(tmp_path, sender)
    asyncio.run(runner.cycle_once())

    recs = _records(tmp_path / "daemon.log")
    assert not any(r["level"] == "DEBUG" for r in recs)


def test_reconcile_failure_logs_error_but_cycle_survives(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("error", filename="daemon.log")

    async def _boom() -> None:
        raise RuntimeError("reconcile упал")

    sender = _Sender(SendResult(sent=0, accepted=0, skipped=0, requeued=0, last_error=None))
    runner = _runner(tmp_path, sender, reconcile=_boom)
    # cycle_once НЕ должен пробросить исключение reconcile.
    asyncio.run(runner.cycle_once())

    recs = _records(tmp_path / "daemon.log")
    errs = [r for r in recs if r["level"] == "ERROR"]
    assert errs, "провал reconcile обязан оставить ERROR-след в daemon.log"
    assert any("reconcile упал" in json.dumps(r, ensure_ascii=False) for r in errs)
