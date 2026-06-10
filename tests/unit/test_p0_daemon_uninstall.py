"""Тесты ``daemon uninstall`` + autostart-uninstall helpers (P0).

Реальный launchctl/systemctl/schtasks НЕ вызывается — только удаление
unit-файла + печать инструкции (обратное к ``daemon install``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from skills_hub_cli import output as output_module
from skills_hub_cli.commands import daemon as daemon_mod
from skills_hub_cli.daemon.autostart import (
    AutostartUninstallResult,
    install_for_platform,
    uninstall_for_platform,
)


def _text_mode() -> None:
    output_module._mode = "text"


# ==================== autostart uninstall (filesystem) ====================
def test_uninstall_macos_removes_plist(tmp_path: Path) -> None:
    # Сначала ставим, потом снимаем — файл должен исчезнуть.
    install_for_platform("macos", home_dir=tmp_path, binary="/opt/skills-hub")
    plist = tmp_path / "Library" / "LaunchAgents" / "com.skills-hub.daemon.plist"
    assert plist.exists()

    result = uninstall_for_platform("macos", home_dir=tmp_path)
    assert isinstance(result, AutostartUninstallResult)
    assert result.platform == "macos"
    assert result.removed is True
    assert result.unit_path == plist
    assert not plist.exists()
    assert any("launchctl unload" in line for line in result.instructions)


def test_uninstall_linux_removes_systemd_unit(tmp_path: Path) -> None:
    install_for_platform("linux", home_dir=tmp_path, binary="/opt/skills-hub")
    unit = tmp_path / ".config" / "systemd" / "user" / "skills-hub-daemon.service"
    assert unit.exists()

    result = uninstall_for_platform("linux", home_dir=tmp_path)
    assert result.platform == "linux"
    assert result.removed is True
    assert not unit.exists()
    assert any("systemctl --user disable" in line for line in result.instructions)


def test_uninstall_windows_removes_task_xml(tmp_path: Path) -> None:
    install_for_platform("windows", home_dir=tmp_path, binary=r"C:\Tools\skills-hub.exe")
    xml = tmp_path / ".skills-hub" / "tasks" / "skills-hub-daemon.xml"
    assert xml.exists()

    result = uninstall_for_platform("windows", home_dir=tmp_path)
    assert result.platform == "windows"
    assert result.removed is True
    assert not xml.exists()
    assert any("schtasks /Delete" in line for line in result.instructions)


def test_uninstall_when_unit_absent_is_idempotent(tmp_path: Path) -> None:
    """Нет unit-файла → removed=False, но инструкция всё равно печатается."""
    result = uninstall_for_platform("linux", home_dir=tmp_path)
    assert result.removed is False
    # Инструкция (как снять, если активировали вручную) всё равно полезна.
    assert result.instructions


def test_uninstall_unknown_platform_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        uninstall_for_platform("solaris", home_dir=tmp_path)


# ==================== cmd_daemon_uninstall ====================
def test_cmd_daemon_uninstall_removes_and_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    # Ставим под Linux в фейковый home.
    install_for_platform("linux", home_dir=tmp_path, binary="/opt/skills-hub")
    unit = tmp_path / ".config" / "systemd" / "user" / "skills-hub-daemon.service"
    assert unit.exists()

    daemon_mod.cmd_daemon_uninstall(platform="linux", home=tmp_path)
    assert not unit.exists()


def test_cmd_daemon_uninstall_invalid_platform() -> None:
    _text_mode()
    import typer

    with pytest.raises(typer.Exit) as exc:
        daemon_mod.cmd_daemon_uninstall(platform="bsd", home=None)
    assert exc.value.exit_code == 1


def test_cmd_daemon_uninstall_json_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    output_module._mode = "json"
    try:
        install_for_platform("linux", home_dir=tmp_path, binary="/opt/skills-hub")
        daemon_mod.cmd_daemon_uninstall(platform="linux", home=tmp_path)
        out = capsys.readouterr().out.strip().splitlines()[-1]
        payload: dict[str, Any] = json.loads(out)
        assert payload["event"] == "autostart_uninstalled"
        assert payload["platform"] == "linux"
        assert payload["removed"] is True
    finally:
        output_module._mode = "text"


def test_daemon_uninstall_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_obj = MagicMock()
    # build_app требует залогиненного user'а для daemon sub-app.
    from skills_hub_cli.config import ClientConfig

    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    _ = cfg_obj
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    daemon_group = next(t for t in app.registered_groups if t.name == "daemon")
    sub_names = [c.name for c in daemon_group.typer_instance.registered_commands]
    assert "uninstall" in sub_names
    assert "install" in sub_names
