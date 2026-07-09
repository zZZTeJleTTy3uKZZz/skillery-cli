"""Аналитика-эпик W1a: Windows Task Scheduler autorestart + boot trigger.

План: в ``<Settings>`` Windows-таски — ``<RestartOnFailure>`` (Interval PT1M,
Count 3), плюс второй триггер ``<BootTrigger>`` (старт при загрузке, не только
логин). macOS (KeepAlive) / linux (Restart=on-failure) уже имеют autorestart —
их шаблоны НЕ трогаем.
"""
from __future__ import annotations

from skillery_cli.daemon.autostart import (
    build_launchd_plist,
    build_systemd_unit,
    build_windows_task_xml,
)


def test_windows_task_has_restart_on_failure() -> None:
    xml = build_windows_task_xml(binary=r"C:\Tools\skillery.exe")
    assert "<RestartOnFailure>" in xml
    assert "<Interval>PT1M</Interval>" in xml
    assert "<Count>3</Count>" in xml
    # RestartOnFailure обязан быть внутри <Settings> (валидатор schtasks строг).
    settings = xml.split("<Settings>", 1)[1].split("</Settings>", 1)[0]
    assert "<RestartOnFailure>" in settings


def test_windows_task_has_boot_trigger() -> None:
    xml = build_windows_task_xml(binary=r"C:\Tools\skillery.exe")
    assert "<BootTrigger>" in xml
    assert "<Enabled>true</Enabled>" in xml
    # BootTrigger внутри <Triggers>, рядом с LogonTrigger.
    triggers = xml.split("<Triggers>", 1)[1].split("</Triggers>", 1)[0]
    assert "<BootTrigger>" in triggers
    assert "<LogonTrigger>" in triggers


def test_windows_task_still_valid_structure() -> None:
    """Базовая структура цела (binary + daemon run)."""
    xml = build_windows_task_xml(binary=r"C:\Tools\skillery.exe")
    assert "skillery.exe" in xml
    assert "daemon run" in xml
    assert xml.count("<Settings>") == 1
    assert xml.count("<Triggers>") == 1


def test_macos_and_linux_templates_untouched() -> None:
    """macOS/linux autorestart НЕ трогаем — RestartOnFailure там не появляется."""
    plist = build_launchd_plist(binary="/x/skillery", log_dir=__import__("pathlib").Path("/tmp"))
    unit = build_systemd_unit(binary="/x/skillery", log_dir=__import__("pathlib").Path("/tmp"))
    # macOS KeepAlive остаётся
    assert "<key>KeepAlive</key>" in plist
    assert "RestartOnFailure" not in plist
    # linux Restart=on-failure остаётся
    assert "Restart=on-failure" in unit
    assert "RestartOnFailure" not in unit
