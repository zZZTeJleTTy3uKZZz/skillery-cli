"""Генерация autostart unit-файлов для daemon'а.

Поддерживаются три платформы:
- **macOS** — launchd ``.plist`` в ``~/Library/LaunchAgents/``.
- **Linux** — systemd user unit в ``~/.config/systemd/user/``.
- **Windows** — XML для ``schtasks /Create /XML`` (Task Scheduler).

Команда ``skillery daemon install`` пишет нужный файл и печатает
**инструкцию** что с ним делать (``launchctl load``,
``systemctl --user enable``, ``schtasks /Create /XML``). Сам install НЕ
вызывает sudo и не модифицирует system-wide settings — это безопасно
для CI / shared dev-окружения. Тоже самое означает, что эта функция
тестируется на mock filesystem.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from skillery_cli import _branding

SERVICE_NAME = _branding.DAEMON_SERVICE_ID
"""Уникальный label для launchd / unit-name для systemd."""

_UNIT_BASENAME = _branding.DAEMON_UNIT_BASENAME  # напр. skillery-daemon
_TASK_NAME = _branding.DAEMON_TASK_NAME  # напр. SkilleryDaemon
_HOME = _branding.HOME_DIR_NAME  # напр. .skillery
_WIN_EOL = chr(13) + chr(10)  # CRLF для .vbs/.cmd в автозагрузке
_DAEMON_DESC = f"{_branding.APP_NAME.capitalize()} event-tracking daemon"


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


def _resolve_cli_binary() -> str:
    """Путь до CLI-бинаря на PATH (иначе — имя команды как fallback)."""
    return shutil.which(_branding.APP_NAME) or _branding.APP_NAME


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
    log_dir = home_dir / _HOME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_dir = home_dir / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{SERVICE_NAME}.plist"
    content = build_launchd_plist(
        binary=binary or _resolve_cli_binary(), log_dir=log_dir
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
Description={desc}
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
    return SYSTEMD_TEMPLATE.format(binary=binary, log_dir=log_dir, desc=_DAEMON_DESC)


def install_systemd(
    *,
    home_dir: Path,
    binary: str | None = None,
) -> AutostartArtifact:
    """Запись ``~/.config/systemd/user/skillery-daemon.service``."""
    log_dir = home_dir / _HOME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    unit_dir = home_dir / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / f"{_UNIT_BASENAME}.service"
    content = build_systemd_unit(
        binary=binary or _resolve_cli_binary(), log_dir=log_dir
    )
    unit_path.write_text(content, encoding="utf-8")
    instructions = [
        f"# Установлен systemd user unit: {unit_path}",
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {_UNIT_BASENAME}.service",
        f"# Снять с autostart: systemctl --user disable --now {_UNIT_BASENAME}.service",
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
    <Description>{desc}</Description>
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
    return WIN_TASK_TEMPLATE.format(binary=binary, desc=_DAEMON_DESC)


def install_windows_task(
    *,
    home_dir: Path,
    binary: str | None = None,
) -> AutostartArtifact:
    """Сохраняет XML для ``schtasks /Create /XML``."""
    task_dir = home_dir / _HOME / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    xml_path = task_dir / f"{_UNIT_BASENAME}.xml"
    content = build_windows_task_xml(
        binary=binary or _resolve_cli_binary()
    )
    xml_path.write_text(content, encoding="utf-16")
    instructions = [
        f"# Сохранён Task Scheduler XML: {xml_path}",
        f'schtasks /Create /TN "{_TASK_NAME}" /XML "{xml_path}"',
        f'# Снять с autostart: schtasks /Delete /TN "{_TASK_NAME}" /F',
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
            home_dir / ".config" / "systemd" / "user" / f"{_UNIT_BASENAME}.service"
        )
    if platform == "windows":
        return home_dir / _HOME / "tasks" / f"{_UNIT_BASENAME}.xml"
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
            f"systemctl --user disable --now {_UNIT_BASENAME}.service",
            "systemctl --user daemon-reload",
        ]
    if platform == "windows":
        return [
            f"# Снят Task Scheduler XML: {unit_path}",
            f'schtasks /Delete /TN "{_TASK_NAME}" /F',
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


# === Активация (не только генерация unit-файла) ===
def _activation_command(platform: str, unit_path: Path) -> list[str] | None:
    """Команда, которая РЕАЛЬНО ставит демон на автозапуск (user-scope, без sudo)."""
    if platform == "macos":
        return ["launchctl", "load", "-w", str(unit_path)]
    if platform == "linux":
        return [
            "systemctl", "--user", "enable", "--now",
            f"{_UNIT_BASENAME}.service",
        ]
    if platform == "windows":
        return [
            "schtasks", "/Create", "/F", "/TN", _TASK_NAME, "/XML", str(unit_path),
        ]
    return None


def activate_autostart(artifact: AutostartArtifact) -> dict[str, object]:
    """Включить автозапуск демона. Идемпотентно, никогда не бросает.

    Раньше ``daemon install`` только ПИСАЛ unit-файл, а активацию оставлял
    пользователю строкой в инструкции — и почти никто её не выполнял: демон не
    переживал перезагрузку, устройство «пропадало» из веба, задания копились в
    очереди. Команды тут user-scope (без sudo, без system-wide изменений).

    Возвращает ``{"activated": bool, "command": …, "error": …}``.
    """
    cmd = _activation_command(artifact.platform, artifact.unit_path)
    if cmd is None:
        return {"activated": False, "command": None, "error": "неизвестная платформа"}
    if shutil.which(cmd[0]) is None:
        return {
            "activated": False,
            "command": " ".join(cmd),
            "error": f"{cmd[0]} не найден в PATH",
        }
    try:
        # systemd требует перечитать юниты перед enable.
        if artifact.platform == "linux":
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                check=False, capture_output=True, timeout=30,
            )
        proc = subprocess.run(cmd, check=False, capture_output=True, timeout=60)
        if proc.returncode == 0:
            return {"activated": True, "command": " ".join(cmd), "error": None}
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip()
        return {"activated": False, "command": " ".join(cmd), "error": err[:300]}
    except Exception as exc:  # автозапуск — не повод валить вход
        return {"activated": False, "command": " ".join(cmd), "error": str(exc)}


def ensure_autostart(*, home_dir: Path | None = None) -> dict[str, object]:
    """Сгенерировать unit-файл И включить автозапуск. Best-effort, идемпотентно."""
    try:
        artifact = install_for_platform(None, home_dir=home_dir or Path.home())
    except Exception as exc:  # noqa: BLE001
        return {"activated": False, "command": None, "error": str(exc)}
    result = activate_autostart(artifact)
    if not result.get("activated") and artifact.platform == "windows":
        # Планировщик отказал (обычно «Access is denied» — задача в корне
        # требует администратора) → кладём скрипт в папку автозагрузки.
        fallback = _install_windows_startup_shortcut(home_dir or Path.home())
        if fallback.get("activated"):
            fallback["platform"] = artifact.platform
            return fallback
    result["unit_path"] = str(artifact.unit_path)
    result["platform"] = artifact.platform
    # Watchdog: периодически поднимаем демон, если он умер посреди сессии и ни
    # одна команда не сработала (login-autostart покрывает только вход). Idem-
    # potent: `daemon start` держит single-instance лок, дублей не будет.
    result["watchdog"] = install_watchdog()
    return result


def _write_watchdog_vbs(home_dir: Path) -> Path:
    """Безоконный .vbs-лаунчер для watchdog-задачи (тот же приём, что автозапуск).

    Task Scheduler, запуская КОНСОЛЬНЫЙ ``skillery.exe daemon start`` напрямую,
    показывает окно консоли на секунду каждый тик (и мелькает «Daemon уже
    запущен (pid=…)»). ``WScript.Shell.Run(..., 0, False)`` (флаг 0 = скрыто)
    окна не создаёт вовсе.
    """
    binary = _resolve_cli_binary()
    home = home_dir / _HOME
    home.mkdir(parents=True, exist_ok=True)
    vbs = home / "skillery-watchdog.vbs"
    quoted = binary.replace('"', '""')
    body = [
        "' Skillery: watchdog — тихо поднимает демон, если умер. Без окна.",
        'CreateObject("WScript.Shell").Run """' + quoted + '"" daemon start", 0, False',
        "",
    ]
    vbs.write_text(_WIN_EOL.join(body), encoding="utf-8")
    return vbs


def install_watchdog(
    *, interval_min: int = 10, home_dir: Path | None = None
) -> dict[str, object]:
    """Периодическая проверка «демон жив» (best-effort, user-scope).

    Windows — Task Scheduler ``/SC MINUTE /MO N`` (без админа, /F перезаписью).
    ⚠️ Задача запускает НЕ консольный ``skillery.exe`` напрямую (Task Scheduler
    показал бы окно консоли каждый тик), а ``wscript.exe //B <vbs>``: wscript —
    GUI-хост (своей консоли нет), а .vbs поднимает демон скрытым флагом. Двойная
    гарантия «без окна».

    Linux — systemd timer уже покрывает Restart; macOS — launchd KeepAlive. На
    не-Windows возвращаем no-op (там автозапуск и так самоперезапускающийся).
    """
    if sys.platform != "win32":
        return {"installed": False, "reason": "не требуется (launchd/systemd)"}
    task = f"{_TASK_NAME}Watchdog"
    try:
        vbs = _write_watchdog_vbs(home_dir or Path.home())
        # /TR = wscript //B (batch, без диалогов) на безоконный .vbs.
        tr = f'wscript.exe //B //Nologo "{vbs}"'
        proc = subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(interval_min),
             "/TN", task, "/TR", tr],
            capture_output=True, timeout=30, check=False,
            creationflags=0x08000000,  # без вспышки у самой команды создания
        )
        ok = proc.returncode == 0
        return {
            "installed": ok,
            "task": task,
            "interval_min": interval_min,
            "launcher": str(vbs),
            "error": None if ok else (proc.stderr or b"").decode("utf-8", "ignore")[:200],
        }
    except Exception as exc:  # noqa: BLE001 — watchdog не критичен
        return {"installed": False, "error": str(exc)}


def _windows_startup_dir(home_dir: Path) -> Path:
    """Пользовательская папка автозагрузки Windows (без админ-прав)."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else home_dir / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _install_windows_startup_shortcut(home_dir: Path) -> dict[str, object]:
    """Fallback автозапуска Windows: СКРЫТЫЙ запуск демона из папки Startup.

    `schtasks /Create` на многих системах отвечает «Access is denied» —
    задача в корне планировщика требует администратора. Папка автозагрузки прав
    не требует и срабатывает при входе пользователя.

    Запуск идёт через .vbs, а НЕ через .cmd: `cmd.exe` при старте показывает
    окно консоли, и `start /b` делу не помогает — он выполняет команду В ТОМ ЖЕ
    окне, поэтому у пользователя болталась вкладка терминала всё время работы
    демона. WScript.Shell.Run с флагом 0 не создаёт окна вообще.
    """
    try:
        binary = _resolve_cli_binary()
        startup = _windows_startup_dir(home_dir)
        startup.mkdir(parents=True, exist_ok=True)

        # Прежняя .cmd-версия оставила бы вторую запись автозагрузки (и то самое
        # окно) — сносим её при обновлении.
        legacy = startup / "skillery-daemon.cmd"
        if legacy.exists():
            legacy.unlink()

        script = startup / "skillery-daemon.vbs"
        quoted = binary.replace('"', '""')
        body = [
            "' Skillery: фоновый запуск демона без окна консоли.",
            'CreateObject("WScript.Shell").Run """' + quoted + '"" daemon start", 0, False',
            "",
        ]
        script.write_text(_WIN_EOL.join(body), encoding="utf-8")
        return {
            "activated": True,
            "command": f"startup: {script}",
            "error": None,
            "unit_path": str(script),
        }
    except Exception as exc:  # noqa: BLE001
        return {"activated": False, "command": None, "error": str(exc)}
