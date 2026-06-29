"""Генерация autostart unit-файлов для daemon'а.

Поддерживаются три платформы:
- **macOS** — launchd ``.plist`` в ``~/Library/LaunchAgents/``.
- **Linux** — systemd user unit в ``~/.config/systemd/user/``.
- **Windows** — XML для ``schtasks /Create /XML`` (Task Scheduler).

Команда ``skills-hub daemon install`` пишет нужный файл и печатает
**инструкцию** что с ним делать (``launchctl load``,
``systemctl --user enable``, ``schtasks /Create /XML``). Сам install НЕ
вызывает sudo и не модифицирует system-wide settings — это безопасно
для CI / shared dev-окружения. Тоже самое означает, что эта функция
тестируется на mock filesystem.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


SERVICE_NAME = "com.skills-hub.daemon"
"""Уникальный label для launchd / unit-name для systemd."""


@dataclass
class AutostartArtifact:
    """Сгенерированный unit-файл + текст инструкции."""

    platform: str
    """``macos`` | ``linux`` | ``windows``"""

    unit_path: Path
    """Куда записан unit-файл."""

    content: str
    """Содержимое unit-файла (тот же текст что в файле)."""

    instructions: list[str]
    """Шаги для пользователя (1 строка = 1 команда / комментарий)."""


def detect_platform() -> str:
    """Возвращает один из ``macos`` / ``linux`` / ``windows``."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    raise RuntimeError(f"Платформа не поддержана: {sys.platform}")


def _resolve_skills_hub_binary() -> str:
    """Полный путь до ``skills-hub`` (на дев-машине / в venv)."""
    found = shutil.which("skills-hub")
    return found or "skills-hub"


# === macOS — launchd ===
LAUNCHD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>daemon</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/daemon.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daemon.err.log</string>
</dict>
</plist>
"""


def build_launchd_plist(*, binary: str, log_dir: Path) -> str:
    return LAUNCHD_TEMPLATE.format(
        label=SERVICE_NAME, binary=binary, log_dir=log_dir
    )


def install_launchd(
    *,
    home_dir: Path,
    binary: str | None = None,
) -> AutostartArtifact:
    """Запись ``~/Library/LaunchAgents/{label}.plist`` + инструкция."""
    log_dir = home_dir / ".skills-hub" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_dir = home_dir / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{SERVICE_NAME}.plist"
    content = build_launchd_plist(
        binary=binary or _resolve_skills_hub_binary(), log_dir=log_dir
    )
    plist_path.write_text(content, encoding="utf-8")
    instructions = [
        f"# Установлен launchd plist: {plist_path}",
        f"launchctl load {plist_path}",
        f"# Снять с autostart: launchctl unload {plist_path}",
    ]
    return AutostartArtifact(
        platform="macos",
        unit_path=plist_path,
        content=content,
        instructions=instructions,
    )


# === Linux — systemd user unit ===
SYSTEMD_TEMPLATE = """[Unit]
Description=Skillery event-tracking daemon
After=network.target

[Service]
Type=simple
ExecStart={binary} daemon run
Restart=on-failure
RestartSec=10
StandardOutput=append:{log_dir}/daemon.out.log
StandardError=append:{log_dir}/daemon.err.log

[Install]
WantedBy=default.target
"""


def build_systemd_unit(*, binary: str, log_dir: Path) -> str:
    return SYSTEMD_TEMPLATE.format(binary=binary, log_dir=log_dir)


def install_systemd(
    *,
    home_dir: Path,
    binary: str | None = None,
) -> AutostartArtifact:
    """Запись ``~/.config/systemd/user/skills-hub-daemon.service``."""
    log_dir = home_dir / ".skills-hub" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    unit_dir = home_dir / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "skills-hub-daemon.service"
    content = build_systemd_unit(
        binary=binary or _resolve_skills_hub_binary(), log_dir=log_dir
    )
    unit_path.write_text(content, encoding="utf-8")
    instructions = [
        f"# Установлен systemd user unit: {unit_path}",
        "systemctl --user daemon-reload",
        "systemctl --user enable --now skills-hub-daemon.service",
        "# Снять с autostart: systemctl --user disable --now skills-hub-daemon.service",
    ]
    return AutostartArtifact(
        platform="linux",
        unit_path=unit_path,
        content=content,
        instructions=instructions,
    )


# === Windows — Task Scheduler XML ===
WIN_TASK_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Skillery event-tracking daemon</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{binary}</Command>
      <Arguments>daemon run</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def build_windows_task_xml(*, binary: str) -> str:
    return WIN_TASK_TEMPLATE.format(binary=binary)


def install_windows_task(
    *,
    home_dir: Path,
    binary: str | None = None,
) -> AutostartArtifact:
    """Сохраняет XML для ``schtasks /Create /XML``."""
    task_dir = home_dir / ".skills-hub" / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    xml_path = task_dir / "skills-hub-daemon.xml"
    content = build_windows_task_xml(
        binary=binary or _resolve_skills_hub_binary()
    )
    xml_path.write_text(content, encoding="utf-16")
    instructions = [
        f"# Сохранён Task Scheduler XML: {xml_path}",
        f'schtasks /Create /TN "SkilleryDaemon" /XML "{xml_path}"',
        '# Снять с autostart: schtasks /Delete /TN "SkilleryDaemon" /F',
    ]
    return AutostartArtifact(
        platform="windows",
        unit_path=xml_path,
        content=content,
        instructions=instructions,
    )


def install_for_platform(
    platform: str | None = None,
    *,
    home_dir: Path,
    binary: str | None = None,
) -> AutostartArtifact:
    """Выбирает installer по платформе.

    Если ``platform=None`` — автодетект через :func:`detect_platform`.
    """
    actual = platform or detect_platform()
    if actual == "macos":
        return install_launchd(home_dir=home_dir, binary=binary)
    if actual == "linux":
        return install_systemd(home_dir=home_dir, binary=binary)
    if actual == "windows":
        return install_windows_task(home_dir=home_dir, binary=binary)
    raise ValueError(f"Неизвестная платформа: {actual}")


# === Uninstall (обратное к install) ===
@dataclass
class AutostartUninstallResult:
    """Результат снятия autostart unit-файла."""

    platform: str
    """``macos`` | ``linux`` | ``windows``"""

    unit_path: Path
    """Путь, по которому ожидался unit-файл."""

    removed: bool
    """``True`` если файл существовал и был удалён; ``False`` если его не было."""

    instructions: list[str]
    """Что сделать пользователю (деактивировать в launchd/systemd/schtasks)."""


def _unit_path_for(platform: str, home_dir: Path) -> Path:
    """Путь unit-файла, который пишет соответствующий ``install_*``.

    Единый источник правды о расположении — переиспользуется install/uninstall.
    """
    if platform == "macos":
        return home_dir / "Library" / "LaunchAgents" / f"{SERVICE_NAME}.plist"
    if platform == "linux":
        return (
            home_dir / ".config" / "systemd" / "user" / "skills-hub-daemon.service"
        )
    if platform == "windows":
        return home_dir / ".skills-hub" / "tasks" / "skills-hub-daemon.xml"
    raise ValueError(f"Неизвестная платформа: {platform}")


def _uninstall_instructions(platform: str, unit_path: Path) -> list[str]:
    """Инструкция как деактивировать autostart (sudo не нужен)."""
    if platform == "macos":
        return [
            f"# Снят launchd plist: {unit_path}",
            f"launchctl unload {unit_path}",
            "# (если демон ещё не выгружен — команда выше остановит autostart)",
        ]
    if platform == "linux":
        return [
            f"# Снят systemd user unit: {unit_path}",
            "systemctl --user disable --now skills-hub-daemon.service",
            "systemctl --user daemon-reload",
        ]
    if platform == "windows":
        return [
            f"# Снят Task Scheduler XML: {unit_path}",
            'schtasks /Delete /TN "SkilleryDaemon" /F',
        ]
    raise ValueError(f"Неизвестная платформа: {platform}")


def uninstall_for_platform(
    platform: str | None = None,
    *,
    home_dir: Path,
) -> AutostartUninstallResult:
    """Снять autostart unit-файл (обратное к :func:`install_for_platform`).

    Удаляет тот же файл, что писал ``install_*`` (user-scope, без sudo). Файла
    нет ⇒ ``removed=False`` (идемпотентно). Печать инструкции (как
    деактивировать в launchd/systemd/schtasks) — задача вызывающей команды.
    """
    actual = platform or detect_platform()
    unit_path = _unit_path_for(actual, home_dir)
    removed = False
    if unit_path.exists():
        unit_path.unlink()
        removed = True
    return AutostartUninstallResult(
        platform=actual,
        unit_path=unit_path,
        removed=removed,
        instructions=_uninstall_instructions(actual, unit_path),
    )
