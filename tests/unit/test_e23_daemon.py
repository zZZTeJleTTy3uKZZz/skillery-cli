"""Тесты daemon lifecycle + autostart unit-files (E23).

Mock-фразу filesystem мы выполняем через `tmp_path` + override
`SKILLERY_CONFIG_DIR`. Реальный fork / launchctl / schtasks НЕ
вызываются — только генерация файлов.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skillery_cli.daemon.autostart import (
    AutostartArtifact,
    build_launchd_plist,
    build_systemd_unit,
    build_windows_task_xml,
    install_launchd,
    install_systemd,
    install_windows_task,
)
from skillery_cli.core import analytics_sync
from skillery_cli.daemon.daemon_runner import (
    DaemonRunner,
    DaemonState,
    read_running_pid,
    read_state,
)
from skillery_cli.daemon.event_sender import OutboxSender


def _make_event_runner(tmp_path: Path, **runner_kwargs) -> DaemonRunner:  # noqa: ANN003
    """Runner с одним событием в ОБЩЕЙ очереди + успешным клиентом.

    #1180: такт демона доставляет общий outbox (``OutboxSender``), своей
    очереди у аналитики больше нет. Контракт такта тот же: sent/accepted.
    """
    analytics_sync.track("skill.install")
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        return {"accepted": len(events)}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close
    fake_client.send_telemetry_batch = None
    sender = OutboxSender(lambda: fake_client, min_interval=0.0)
    return DaemonRunner(
        sender,
        interval_seconds=60,
        pid_path=tmp_path / "daemon.pid",
        state_path=tmp_path / "daemon.state.json",
        **runner_kwargs,
    )


# ========== DaemonRunner ==========
@pytest.mark.asyncio
async def test_daemon_runner_cycle_once_persists_state(tmp_path: Path) -> None:
    runner = _make_event_runner(tmp_path)
    last = await runner.cycle_once()
    assert last["sent"] == 1
    assert last["accepted"] == 1
    assert analytics_sync.pending_count() == 0
    # State-файл сохраняется на каждом цикле
    state = read_state(runner.state_path)
    assert state["cycles"] == 1
    assert state["total_sent"] == 1


@pytest.mark.asyncio
async def test_daemon_reconcile_callback_invoked_after_send(
    tmp_path: Path,
) -> None:
    """Best-effort reconcile вызывается ПОСЛЕ send_once в каждом такте."""
    calls: list[int] = []

    async def _reconcile() -> None:
        calls.append(1)

    runner = _make_event_runner(tmp_path, reconcile=_reconcile)
    last = await runner.cycle_once()
    # Event-flow не пострадал.
    assert last["sent"] == 1
    assert last["accepted"] == 1
    # Reconcile отработал.
    assert calls == [1]


@pytest.mark.asyncio
async def test_daemon_reconcile_failure_does_not_break_cycle(
    tmp_path: Path,
) -> None:
    """Падение reconcile глотается (suppress) — event-цикл продолжает работать."""

    async def _boom() -> None:
        raise RuntimeError("reconcile blew up")

    runner = _make_event_runner(tmp_path, reconcile=_boom)
    # Не должно бросить, событие всё равно отправлено.
    last = await runner.cycle_once()
    assert last["sent"] == 1
    assert last["accepted"] == 1
    # State по-прежнему пишется (цикл завершился штатно).
    state = read_state(runner.state_path)
    assert state["cycles"] == 1


def test_read_running_pid_missing_returns_none(tmp_path: Path) -> None:
    pid = read_running_pid(tmp_path / "absent.pid")
    assert pid is None


def test_read_running_pid_parses_int(tmp_path: Path) -> None:
    p = tmp_path / "daemon.pid"
    p.write_text("12345", encoding="utf-8")
    assert read_running_pid(p) == 12345


def test_read_state_missing(tmp_path: Path) -> None:
    assert read_state(tmp_path / "absent.json") == {}


# ========== autostart unit-file generation ==========
def test_build_launchd_plist_includes_label_and_binary() -> None:
    content = build_launchd_plist(
        binary="/usr/local/bin/skillery", log_dir=Path("/tmp/logs")
    )
    assert "com.skillery.daemon" in content
    assert "/usr/local/bin/skillery" in content
    assert "<key>RunAtLoad</key>" in content


def test_install_launchd_writes_plist_in_home(tmp_path: Path) -> None:
    artifact = install_launchd(home_dir=tmp_path, binary="/opt/skillery")
    assert isinstance(artifact, AutostartArtifact)
    assert artifact.platform == "macos"
    expected = tmp_path / "Library" / "LaunchAgents" / "com.skillery.daemon.plist"
    assert artifact.unit_path == expected
    assert expected.exists()
    text = expected.read_text(encoding="utf-8")
    assert "/opt/skillery" in text
    assert any("launchctl load" in line for line in artifact.instructions)


def test_build_systemd_unit_includes_exec_start() -> None:
    content = build_systemd_unit(
        binary="/usr/bin/skillery", log_dir=Path("/var/log/sh")
    )
    assert "ExecStart=/usr/bin/skillery daemon run" in content
    assert "[Install]" in content
    assert "WantedBy=default.target" in content


def test_install_systemd_writes_unit_in_home(tmp_path: Path) -> None:
    artifact = install_systemd(home_dir=tmp_path, binary="/opt/skillery")
    assert artifact.platform == "linux"
    expected = tmp_path / ".config" / "systemd" / "user" / "skillery-daemon.service"
    assert artifact.unit_path == expected
    text = expected.read_text(encoding="utf-8")
    assert "/opt/skillery" in text
    assert any("systemctl --user enable" in line for line in artifact.instructions)


def test_build_windows_task_xml_includes_command() -> None:
    content = build_windows_task_xml(binary=r"C:\Tools\skillery.exe")
    # XML uses generic <Command>; ensure binary inside
    assert "skillery.exe" in content
    assert "<LogonTrigger>" in content
    assert "daemon run" in content


def test_install_windows_task_writes_xml(tmp_path: Path) -> None:
    artifact = install_windows_task(
        home_dir=tmp_path, binary=r"C:\Tools\skillery.exe"
    )
    assert artifact.platform == "windows"
    expected = tmp_path / ".skillery" / "tasks" / "skillery-daemon.xml"
    assert artifact.unit_path == expected
    text = expected.read_text(encoding="utf-16")
    assert "skillery.exe" in text
    assert any("schtasks" in line for line in artifact.instructions)


# ========== daemon default paths follow centralized config dir ==========
def _isolate_home_no_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("SKILLERY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SKILLERY_STORE_DIR", raising=False)
    monkeypatch.delenv("SKILLERY_PROFILE", raising=False)
    from skillery_cli import config as config_mod

    config_mod.set_active_profile(None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_daemon_default_paths_use_skillery_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pid/state/guard/очередь живут в ребренд-каталоге ~/.skillery (не хардкод)."""
    from telemetrykit import outbox

    from skillery_cli.core.analytics_sync import legacy_queue_path
    from skillery_cli.daemon.daemon_runner import (
        default_guard_path,
        default_pid_path,
        default_state_path,
    )

    home = _isolate_home_no_env(tmp_path, monkeypatch)
    base = home / ".skillery"
    assert default_pid_path() == base / "daemon.pid"
    assert default_state_path() == base / "daemon.state.json"
    assert default_guard_path() == base / "events.guard.json"
    # #1180: очередь ОДНА — общий outbox; ``events.queue.json`` остался только
    # как источник миграции и рядом с ним же.
    assert outbox.path() == base / "outbox.jsonl"
    assert legacy_queue_path() == base / "events.queue.json"


def test_daemon_default_paths_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если есть только legacy ~/.skillery — daemon пишет туда (compat)."""
    from skillery_cli.daemon.daemon_runner import default_guard_path

    home = _isolate_home_no_env(tmp_path, monkeypatch)
    (home / ".skillery").mkdir()
    assert default_guard_path() == home / ".skillery" / "events.guard.json"


def test_daemon_default_paths_respect_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env ``SKILLERY_CONFIG_DIR`` остаётся override-точкой и для daemon."""
    from skillery_cli.daemon.daemon_runner import default_pid_path

    _isolate_home_no_env(tmp_path, monkeypatch)
    override = tmp_path / "cfg"
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(override))
    assert default_pid_path() == override / "daemon.pid"
