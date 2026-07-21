"""Автономный worker обновления CLI. Запускается ОТДЕЛЬНЫМ процессом.

Почему отдельный файл, а не строка в ``python -c`` и не обычный модуль:

* worker переживает ЗАМЕНУ пакета — во время апгрейда `skillery_cli` физически
  перезаписывается, поэтому worker не имеет права ничего из него импортировать;
* он копируется в ``~/.skillery/`` (вне tool-каталога): интерпретатор и скрипт
  внутри ``…/tools/skillery-cli/`` держали бы этот каталог, и `uv` не смог бы
  его удалить — ровно так апгрейд молча падал с «os error 5».

Отсюда же вынужденный дубль логики поиска процессов демона (в пакете она живёт
в ``daemon/single_instance.py``): импортировать её здесь нельзя по той же
причине. Дубль намеренный и локальный — держим его минимальным.

Порядок работы (всё best-effort, апгрейд важнее аккуратности):
1. взять single-flight лок — два одновременных апгрейда рвут trampoline;
2. подождать, пока выйдет породивший нас launcher;
3. погасить ВСЮ цепочку демона (он держит файлы окружения);
4. пройти цепочку команд обновления до первой удачной;
5. поднять демон заново уже НОВЫМ бинарём.

Конфиг приходит одним JSON-файлом (см. ``main``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
_ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Global\\SkilleryUpgradeSingleton"
LOCK_FILENAME = "upgrade.lock"  # POSIX-плечо лока; путь считает lock_path()

IS_WIN = sys.platform == "win32"


def lock_path() -> Path:
    """Файл лока на POSIX. Общий с проверкой в CLI — иначе они бы не встретились."""
    return Path.home() / ".skillery" / LOCK_FILENAME


def _log(msg: str) -> None:
    """Дневник апгрейда: worker фоновый и невидимый, без лога он — чёрный ящик.

    Именно из-за этого «обновление запущено → и тишина» диагностировалось только
    по внешним симптомам. Пишем ход в ~/.skillery/upgrade.log (best-effort).
    """
    try:
        p = Path.home() / ".skillery" / "upgrade.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _no_window_kwargs() -> dict:
    """Флаги, чтобы дочерний процесс не показал консольное окно.

    Мы сами detached, консоли у нас НЕТ. Если запустить консольный uv/git без
    флагов, Windows 11 отдаёт консоль ребёнка терминалу по умолчанию и на экране
    всплывает вкладка Windows Terminal.
    """
    kw: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if IS_WIN:
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


# --------------------------------------------------------------------------
#  single-flight
# --------------------------------------------------------------------------
def acquire_lock():
    """Занять лок апгрейда. Возвращает handle или ``None``, если уже идёт.

    Держится ядром до конца процесса: аварийное завершение worker'а не оставляет
    «залипший» лок, из-за которого апгрейд потом не запустился бы никогда.
    """
    if IS_WIN:
        try:
            import ctypes

            # use_last_error обязателен: через ctypes.windll код ошибки теряется
            # (маршалинг затирает last-error), и ERROR_ALREADY_EXISTS не долетает.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p
            ]
            handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
            if not handle or ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                return None
            return handle
        except Exception:
            return True  # ctypes недоступен — не блокируем апгрейд совсем
    try:
        import fcntl

        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return fh
    except Exception:
        return True


# --------------------------------------------------------------------------
#  остановка демона (вынужденный дубль single_instance — см. модуль-docstring)
# --------------------------------------------------------------------------
def find_daemon_pids() -> list[int]:
    """PID ВСЕХ процессов демона, а не только записанного в PID-файл.

    Одна логическая копия демона на Windows — это цепочка процессов
    (launcher-трамплин → venv-редиректор → базовый интерпретатор), и держат
    файлы окружения они все.
    """
    if IS_WIN:
        script = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "($_.Name -eq 'skillery.exe' -or $_.Name -eq 'python.exe' "
            "-or $_.Name -eq 'pythonw.exe') "
            "-and $_.CommandLine -like '*skillery*' "
            "-and $_.CommandLine -like '*daemon*run*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        cmd = ["pgrep", "-f", r"skillery.*daemon run"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False,
                              **({"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}))
    except Exception:
        return []
    out = (proc.stdout or b"").decode("utf-8", "ignore")
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit() and int(line) != os.getpid():
            pids.append(int(line))
    return pids


def stop_daemons(timeout: float = 10.0) -> list[int]:
    """Погасить всех и ДОЖДАТЬСЯ, пока отпустят файлы.

    Ждать обязательно: `uv tool install --force` падает с «os error 5», если
    хоть один процесс ещё держит `Scripts/`.
    """
    killed: list[int] = []
    for pid in find_daemon_pids():
        try:
            if IS_WIN:
                import ctypes

                PROCESS_TERMINATE = 0x0001
                h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if h:
                    ctypes.windll.kernel32.TerminateProcess(h, 1)
                    ctypes.windll.kernel32.CloseHandle(h)
                    killed.append(pid)
            else:
                import signal

                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
        except Exception:
            continue
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not find_daemon_pids():
            break
        time.sleep(0.3)
    return killed


# --------------------------------------------------------------------------
#  апгрейд + возврат демона
# --------------------------------------------------------------------------
def run_upgrade(commands: list[list[str]], retries: int = 6, delay: float = 15.0) -> bool:
    """Прогнать команды до первой удачной, с ПОВТОРАМИ на первой (пин версии).

    Первая команда — пин точной версии (её мы уже видели на PyPI). Сразу после
    релиза индекс ещё не разъехался по CDN и пин транзиентно отвечает «no
    version». Раньше мы на этом откатывались на голый `install latest` — но
    отставший edge-узел отдавал СТАРУЮ версию как latest, и апгрейд «успешно»
    ставил то же самое. Поэтому пин ПОВТОРЯЕМ с бэкоффом (CDN догоняет за
    секунды-минуты), и только если он так и не дался — пробуем прочие команды
    как последнее средство.
    """
    primary, rest = (commands[0], commands[1:]) if commands else (None, [])
    if primary is not None:
        for attempt in range(1, retries + 1):
            try:
                rc = subprocess.run(primary, timeout=600, **_no_window_kwargs()).returncode
                _log(f"upgrade attempt {attempt}/{retries}: {' '.join(primary)} → rc={rc}")
                if rc == 0:
                    return True
            except Exception as e:  # noqa: BLE001
                _log(f"upgrade attempt {attempt}/{retries} EXC: {e}")
            if attempt < retries:
                time.sleep(delay)
    for cmd in rest:
        try:
            rc = subprocess.run(cmd, timeout=600, **_no_window_kwargs()).returncode
            _log(f"upgrade fallback: {' '.join(cmd)} → rc={rc}")
            if rc == 0:
                return True
        except Exception as e:  # noqa: BLE001
            _log(f"upgrade fallback EXC: {e}")
    return False


def start_daemon(binary: str) -> bool:
    """Поднять демон НОВЫМ бинарём — сразу, не дожидаясь команды пользователя.

    С ПОВТОРАМИ: сразу после апгрейда trampoline `skillery.exe` пересоздаётся и
    несколько секунд может быть в переходном состоянии («uv trampoline failed to
    canonicalize script path»). Запускаем СИНХРОННО и проверяем returncode —
    Popen «выстрелил и забыл» не отличал бы успех от битого бинаря. `daemon
    start` быстрый: сам поднимает detached-демон и выходит.
    """
    if not binary:
        _log("start_daemon: пустой daemon_binary — пропуск")
        return False
    kw = dict(_no_window_kwargs())  # без окна; ждём завершения самой команды
    for attempt in range(1, 6):
        try:
            rc = subprocess.run(
                [binary, "daemon", "start"], timeout=60, **kw
            ).returncode
            _log(f"start_daemon attempt {attempt}: rc={rc}")
            if rc == 0:
                return True
        except Exception as e:  # noqa: BLE001
            _log(f"start_daemon attempt {attempt} EXC: {e}")
        time.sleep(3)
    return False


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    try:
        cfg = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"worker start: bad config: {e}")
        return 2

    lock = acquire_lock()
    if lock is None:
        _log("worker start: апгрейд уже идёт — выходим")
        return 0  # апгрейд уже идёт — второй worker не нужен

    _log(f"--- worker start pid={os.getpid()} ---")
    time.sleep(float(cfg.get("delay", 4.0)))
    killed = stop_daemons()
    _log(f"stop_daemons killed={killed}")
    ok = run_upgrade([list(c) for c in cfg.get("commands", [])])
    _log(f"run_upgrade → {ok}")
    # Демон возвращаем в ЛЮБОМ случае: даже если обновиться не вышло, оставлять
    # пользователя без демона нельзя — очередь заданий перестанет применяться.
    start_daemon(cfg.get("daemon_binary", ""))
    _log(f"--- worker done ok={ok} ---")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
