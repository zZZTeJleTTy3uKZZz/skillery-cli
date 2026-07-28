"""E7 офлайн-batch — `DaemonRunner` применяет backoff между неудачами.

После цикла, в котором flush провалился (sender вернул requeued>0, accepted=0),
runner спит дольше (backoff растёт). На первом успешном цикле backoff
сбрасывается обратно к базовому интервалу. Сам sleep не выполняется —
перехватываем timeout, который runner передаёт в ожидание stop-события.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from skillery_cli.daemon.daemon_runner import DaemonRunner


class _ScriptedSender:
    """Sender-двойник: по очереди отдаёт заранее заданные SendResult'ы."""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self._i = 0

    async def send_once(self):  # noqa: ANN201
        r = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return r


def _run_n_cycles(runner: DaemonRunner, n: int, waits: list[float]) -> None:
    """Гоняем ровно n циклов, перехватывая timeout в ожидании stop."""

    async def _go() -> None:
        original_wait_for = asyncio.wait_for

        async def _fake_wait_for(coro, timeout):  # noqa: ANN001
            # закрываем корутину stop().wait(), чтобы не висела
            if hasattr(coro, "close"):
                coro.close()
            waits.append(timeout)
            if len(waits) >= n:
                runner.stop()
            # имитируем «таймаут истёк» — следующий цикл
            raise asyncio.TimeoutError

        asyncio.wait_for = _fake_wait_for  # type: ignore[assignment]
        try:
            await runner.run_forever()
        finally:
            asyncio.wait_for = original_wait_for  # type: ignore[assignment]

    asyncio.run(_go())


def _make_result(*, requeued: int, accepted: int):  # noqa: ANN202
    from skillery_cli.daemon.event_sender import SendResult

    return SendResult(
        sent=requeued + accepted, accepted=accepted, skipped=0,
        requeued=requeued, last_error="net" if requeued else None,
    )


def test_runner_grows_wait_on_consecutive_failures(tmp_path: Path) -> None:
    # 3 подряд провала flush → wait растёт экспоненциально (60,120,240).
    sender = _ScriptedSender([_make_result(requeued=1, accepted=0)] * 3)
    runner = DaemonRunner(
        sender,  # type: ignore[arg-type]
        interval_seconds=60,
        pid_path=tmp_path / "d.pid",
        state_path=tmp_path / "d.state.json",
    )
    waits: list[float] = []
    _run_n_cycles(runner, 3, waits)
    # 1-й цикл (1-й провал) → 120, 2-й → 240, 3-й → 480.
    assert waits[0] == 120.0
    assert waits[1] == 240.0
    assert waits[2] == 480.0


def test_runner_resets_wait_on_success(tmp_path: Path) -> None:
    # провал, провал, успех → 120, 240, затем сброс к 60.
    sender = _ScriptedSender(
        [
            _make_result(requeued=1, accepted=0),
            _make_result(requeued=1, accepted=0),
            _make_result(requeued=0, accepted=1),
        ]
    )
    runner = DaemonRunner(
        sender,  # type: ignore[arg-type]
        interval_seconds=60,
        pid_path=tmp_path / "d.pid",
        state_path=tmp_path / "d.state.json",
    )
    waits: list[float] = []
    _run_n_cycles(runner, 3, waits)
    assert waits[0] == 120.0
    assert waits[1] == 240.0
    assert waits[2] == 60.0  # успех → backoff сброшен


def test_runner_no_backoff_when_all_success(tmp_path: Path) -> None:
    sender = _ScriptedSender([_make_result(requeued=0, accepted=1)] * 3)
    runner = DaemonRunner(
        sender,  # type: ignore[arg-type]
        interval_seconds=60,
        pid_path=tmp_path / "d.pid",
        state_path=tmp_path / "d.state.json",
    )
    waits: list[float] = []
    _run_n_cycles(runner, 3, waits)
    assert waits == [60.0, 60.0, 60.0]


def test_runner_empty_queue_does_not_trigger_backoff(tmp_path: Path) -> None:
    """Пустая очередь (sent=0) — НЕ провал, backoff не растёт."""
    sender = _ScriptedSender([_make_result(requeued=0, accepted=0)] * 2)
    runner = DaemonRunner(
        sender,  # type: ignore[arg-type]
        interval_seconds=60,
        pid_path=tmp_path / "d.pid",
        state_path=tmp_path / "d.state.json",
    )
    waits: list[float] = []
    _run_n_cycles(runner, 2, waits)
    assert waits == [60.0, 60.0]
