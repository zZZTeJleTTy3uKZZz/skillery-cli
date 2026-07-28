"""``daemon status`` предупреждает о непустой очереди при мёртвом демоне.

План (аналитика-эпик W1a): если ``alive=false`` И ``queue_size>0`` → явное
предупреждение «демон не запущен, N событий не отправлены: skillery daemon
start» + JSON-поля (``warning`` / ``stalled_events``), чтобы автоматика тоже это
видела.

#1180: очередь на машине ОДНА (общий outbox), поэтому status показывает именно
её — вместе с разбивкой по ``kind``, иначе «застряло 12» не отвечает на вопрос
«что именно застряло: логи, запуски навыков или аналитика установок».
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli import output as out_mod
from skillery_cli.commands import daemon as daemon_mod
from skillery_cli.core import analytics_sync


def _wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
          alive: bool, queued: int) -> None:
    for i in range(queued):
        analytics_sync.track(
            "skill.enable", resource_type="skill", resource_id=str(i)
        )
    monkeypatch.setattr(daemon_mod, "default_pid_path", lambda: tmp_path / "d.pid")
    monkeypatch.setattr(daemon_mod, "read_state", lambda *a, **k: {})
    monkeypatch.setattr(
        daemon_mod, "read_running_pid", lambda *a, **k: (123 if alive else None)
    )
    monkeypatch.setattr(daemon_mod, "is_process_alive", lambda pid: alive)
    monkeypatch.setattr(out_mod, "_mode", "json")


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


def test_status_counts_the_shared_queue_not_only_analytics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Очередь одна: в размер входят и логи, и запуски навыков, и аналитика."""
    from telemetrykit import outbox

    _wire(monkeypatch, tmp_path, alive=False, queued=1)
    outbox.append("skill_run", {"skill": "atlas"})
    outbox.append("log", {"level": "error", "message": "провал"})

    daemon_mod.cmd_daemon_status()
    p = _last_payload(capsys)

    assert p["queue_size"] == 3
    assert p["queue_by_kind"] == {
        "analytics_event": 1, "skill_run": 1, "log": 1,
    }
    # Путь ведёт в ОБЩИЙ outbox, а не в снесённую третью очередь.
    assert p["queue_path"].endswith("outbox.jsonl")
    assert "events.queue.json" not in p["queue_path"]


def test_status_survives_unreadable_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Диагностика не имеет права падать из-за недоступной очереди."""
    import telemetrykit.outbox as ob

    _wire(monkeypatch, tmp_path, alive=True, queued=0)

    def _boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(ob, "read_batch", _boom)
    daemon_mod.cmd_daemon_status()
    p = _last_payload(capsys)
    assert p["queue_size"] == 0
    assert p["alive"] is True
