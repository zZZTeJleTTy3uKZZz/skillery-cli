"""Тесты daemon lifecycle + autostart unit-files (E23).

Mock-фразу filesystem мы выполняем через `tmp_path` + override
`SKILLS_HUB_CONFIG_DIR`. Реальный fork / launchctl / schtasks НЕ
вызываются — только генерация файлов.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skills_hub_cli.daemon.autostart import (
    AutostartArtifact,
    build_launchd_plist,
    build_systemd_unit,
    build_windows_task_xml,
    install_launchd,
    install_systemd,
    install_windows_task,
)
from skills_hub_cli.daemon.daemon_runner import (
    DaemonRunner,
    DaemonState,
    read_running_pid,
    read_state,
)
from skills_hub_cli.daemon.event_collector import EventCollector
from skills_hub_cli.daemon.event_sender import EventSender


# ========== DaemonRunner ==========
@pytest.mark.asyncio
async def test_daemon_runner_cycle_once_persists_state(tmp_path: Path) -> None:
    collector = EventCollector(tmp_path / "q.json")
    collector.append("skill.install")
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        return {"accepted": len(events), "event_ids": [f"ev_{i}" for i in range(len(events))]}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close
    sender = EventSender(collector, lambda: fake_client)
    runner = DaemonRunner(
        sender,
        interval_seconds=60,
        pid_path=tmp_path / "daemon.pid",
        state_path=tmp_path / "daemon.state.json",
    )
    last = await runner.cycle_once()
    assert last["sent"] == 1
    assert last["accepted"] == 1
    # State-файл сохраняется на каждом цикле
    state = read_state(runner.state_path)
    assert state["cycles"] == 1
    assert state["total_sent"] == 1


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
        binary="/usr/local/bin/skills-hub", log_dir=Path("/tmp/logs")
    )
    assert "com.skills-hub.daemon" in content
    assert "/usr/local/bin/skills-hub" in content
    assert "<key>RunAtLoad</key>" in content


def test_install_launchd_writes_plist_in_home(tmp_path: Path) -> None:
    artifact = install_launchd(home_dir=tmp_path, binary="/opt/skills-hub")
    assert isinstance(artifact, AutostartArtifact)
    assert artifact.platform == "macos"
    expected = tmp_path / "Library" / "LaunchAgents" / "com.skills-hub.daemon.plist"
    assert artifact.unit_path == expected
    assert expected.exists()
    text = expected.read_text(encoding="utf-8")
    assert "/opt/skills-hub" in text
    assert any("launchctl load" in line for line in artifact.instructions)


def test_build_systemd_unit_includes_exec_start() -> None:
    content = build_systemd_unit(
        binary="/usr/bin/skills-hub", log_dir=Path("/var/log/sh")
    )
    assert "ExecStart=/usr/bin/skills-hub daemon run" in content
    assert "[Install]" in content
    assert "WantedBy=default.target" in content


def test_install_systemd_writes_unit_in_home(tmp_path: Path) -> None:
    artifact = install_systemd(home_dir=tmp_path, binary="/opt/skills-hub")
    assert artifact.platform == "linux"
    expected = tmp_path / ".config" / "systemd" / "user" / "skills-hub-daemon.service"
    assert artifact.unit_path == expected
    text = expected.read_text(encoding="utf-8")
    assert "/opt/skills-hub" in text
    assert any("systemctl --user enable" in line for line in artifact.instructions)


def test_build_windows_task_xml_includes_command() -> None:
    content = build_windows_task_xml(binary=r"C:\Tools\skills-hub.exe")
    # XML uses generic <Command>; ensure binary inside
    assert "skills-hub.exe" in content
    assert "<LogonTrigger>" in content
    assert "daemon run" in content


def test_install_windows_task_writes_xml(tmp_path: Path) -> None:
    artifact = install_windows_task(
        home_dir=tmp_path, binary=r"C:\Tools\skills-hub.exe"
    )
    assert artifact.platform == "windows"
    expected = tmp_path / ".skills-hub" / "tasks" / "skills-hub-daemon.xml"
    assert artifact.unit_path == expected
    text = expected.read_text(encoding="utf-16")
    assert "skills-hub.exe" in text
    assert any("schtasks" in line for line in artifact.instructions)
