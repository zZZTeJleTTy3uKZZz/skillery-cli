"""Аналитика-эпик W1a: daemon status предупреждает о непуст. очереди при
мёртвом демоне.

План: если ``alive=false`` И ``queue_size>0`` → явное предупреждение «демон не
запущен, N событий не отправлены: skillery daemon start» + JSON-поле
(``warning`` / ``stalled_events``), чтобы автоматика тоже это видела.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli import output as out_mod
from skillery_cli.commands import daemon as daemon_mod
from skillery_cli.daemon.event_collector import EventCollector


def _wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
          alive: bool, queued: int) -> EventCollector:
    queue_path = tmp_path / "events.queue.json"
    coll = EventCollector(queue_path)
    for i in range(queued):
        coll.append("skill.enable", resource_type="skill", resource_id=str(i))
    monkeypatch.setattr(daemon_mod, "default_queue_path", lambda: queue_path)
    monkeypatch.setattr(daemon_mod, "default_pid_path", lambda: tmp_path / "d.pid")
    monkeypatch.setattr(daemon_mod, "read_state", lambda *a, **k: {})
    monkeypatch.setattr(
        daemon_mod, "read_running_pid", lambda *a, **k: (123 if alive else None)
    )
    monkeypatch.setattr(daemon_mod, "is_process_alive", lambda pid: alive)
    monkeypatch.setattr(out_mod, "_mode", "json")
    return coll


def _last_payload(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_status_warns_when_dead_and_queue_nonempty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(monkeypatch, tmp_path, alive=False, queued=3)
    daemon_mod.cmd_daemon_status()
    p = _last_payload(capsys)
    assert p["alive"] is False
    assert p["queue_size"] == 3
    # JSON-поле для автоматики
    assert p.get("warning")
    assert p.get("stalled_events") == 3
    assert "daemon start" in p["warning"]


def test_status_no_warning_when_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(monkeypatch, tmp_path, alive=True, queued=3)
    daemon_mod.cmd_daemon_status()
    p = _last_payload(capsys)
    assert p["alive"] is True
    # демон жив → не паникуем, даже если очередь временно непуста
    assert not p.get("warning")
    assert not p.get("stalled_events")


def test_status_no_warning_when_dead_but_queue_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(monkeypatch, tmp_path, alive=False, queued=0)
    daemon_mod.cmd_daemon_status()
    p = _last_payload(capsys)
    assert p["alive"] is False
    assert p["queue_size"] == 0
    # нечего отправлять → предупреждать не о чем
    assert not p.get("warning")
    assert not p.get("stalled_events")
