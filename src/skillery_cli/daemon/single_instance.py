"""Гарантия единственного экземпляра демона + поиск «осиротевших» процессов.

Почему не PID-файл. Раньше единственность держалась на нём: прочитали PID,
проверили «жив ли», решили запускать. Между проверкой и запуском — окно гонки,
и при нескольких источниках старта (автозапуск, самолечение при команде, ручной
`daemon start`) демоны плодились: пользователь закрывал одно окно — появлялись
два. PID-файл вдобавок врёт после жёсткого kill'а и переиспользования PID
системой.

Здесь единственность обеспечивает ЯДРО, а не наш код:
- **Windows** — named mutex (`CreateMutexW`): второй процесс сразу получает
  ``ERROR_ALREADY_EXISTS``;
- **POSIX** — эксклюзивный неблокирующий ``flock`` на файле; при падении
  процесса блокировка снимается операционной системой сама.

Оба механизма отпускаются вместе с процессом — «залипшего» состояния, из-за
которого демон потом не смог бы стартовать, не остаётся.

Перечисление процессов (``powershell Get-CimInstance`` / ``pgrep``) идёт через
:func:`librarykit.proc.run` (#1144): опрос зовётся ПЕРИОДИЧЕСКИ, и без
``CREATE_NO_WINDOW`` каждый тик мигал бы консольным окном powershell.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from librarykit.proc import run as proc_run

__all__ = [
    "DaemonLock",
    "acquire_daemon_lock",
    "find_daemon_pids",
    "is_daemon_locked",
]

_MUTEX_NAME = "Global\\SkilleryDaemonSingleton"
_ERROR_ALREADY_EXISTS = 183

#: env-оверрайд имени ядерного лока. Нужен ТЕСТАМ: мьютекс — объект ядра, он не
#: живёт в HOME, поэтому изоляция HOME его не покрывает. Прогон, взявший боевое
#: имя, заставлял НАСТОЯЩИЙ демон/апгрейдер считать «экземпляр уже работает».
_MUTEX_ENV = "SKILLERY_DAEMON_MUTEX"


def default_mutex_name() -> str:
    """Имя ядерного мьютекса демона (env-оверрайд для изоляции тестов)."""
    return os.environ.get(_MUTEX_ENV) or _MUTEX_NAME


class DaemonLock:
    """Владение локом. Держится, пока жив объект (и процесс)."""

    def __init__(self, handle: object | None, path: Path | None) -> None:
        self._handle = handle
        self._path = path

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def release(self) -> None:
        """Явное освобождение (ядро освободит и само при выходе процесса)."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import ctypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle(ctypes.c_void_p(handle))
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[arg-type]
                handle.close()  # type: ignore[union-attr]
        except Exception:
            return


def _lock_path() -> Path:
    from skillery_cli import _branding

    home = Path.home() / _branding.HOME_DIR_NAME
    home.mkdir(parents=True, exist_ok=True)
    return home / "daemon.lock"


def acquire_daemon_lock(
    *, mutex_name: str | None = None, lock_path: Path | None = None
) -> DaemonLock:
    """Занять лок демона. ``acquired=False`` ⇒ экземпляр уже работает.

    ``mutex_name`` / ``lock_path`` переопределяются только в тестах — так они
    работают на изолированном имени и не зависят от того, крутится ли на машине
    настоящий демон (он же может подняться из самолечения посреди прогона).
    """
    if sys.platform == "win32":
        try:
            import ctypes

            # ВАЖНО: use_last_error=True + ctypes.get_last_error(). Через
            # ctypes.windll.kernel32.GetLastError() код терялся: между
            # CreateMutexW и GetLastError ctypes делает собственные Win32-вызовы
            # (маршалинг), и last-error успевал обнулиться. ERROR_ALREADY_EXISTS
            # не долетал — КАЖДЫЙ демон считал, что взял лок первым, и их
            # выживало несколько. Теперь читаем ошибку сразу через ctypes,
            # который сохраняет её на своей стороне.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p
            ]
            handle = kernel32.CreateMutexW(
                None, True, mutex_name or default_mutex_name()
            )
            last_error = ctypes.get_last_error()
            if not handle:
                return DaemonLock(None, None)
            if last_error == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                return DaemonLock(None, None)
            return DaemonLock(handle, None)
        except Exception:
            # Нет ctypes/доступа — не блокируем запуск совсем, откатываемся на
            # прежнее поведение (PID-файл), оно хотя бы не хуже прежнего.
            return DaemonLock(object(), None)

    try:
        import fcntl

        path = lock_path or _lock_path()
        fh = path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return DaemonLock(None, path)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return DaemonLock(fh, path)
    except Exception:
        return DaemonLock(object(), None)


def is_daemon_locked() -> bool:
    """Уже работает ли демон (проверка без удержания лока)."""
    lock = acquire_daemon_lock()
    if not lock.acquired:
        return True
    lock.release()
    return False


def _windows_daemon_pids() -> list[int]:
    """PID процессов демона по командной строке (не трогая CLI-команды)."""
    # ВАЖНО: фильтруем и по ИМЕНИ процесса, и по строке запуска. Матч только по
    # «daemon run» ловит посторонние процессы (например, шелл, в командной
    # строке которого эти слова просто встречаются) — и мы бы их убили.
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'skillery.exe' -or $_.Name -eq 'python.exe' "
        "-or $_.Name -eq 'pythonw.exe') "
        "-and $_.CommandLine -like '*skillery*' "
        "-and $_.CommandLine -like '*daemon*run*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        # 30s — powershell стартует медленно (загрузка CLR), но CIM-запрос
        # локальный: дольше этого он уже висит, а не работает.
        proc = proc_run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=30,
        )
    except Exception:
        return []
    out = proc.stdout or ""
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit() and int(line) != os.getpid():
            pids.append(int(line))
    return pids


def _posix_daemon_pids() -> list[int]:
    try:
        # Тот же принцип, что и на Windows: только НАШ процесс, а не любой,
        # где в аргументах попались эти слова.
        proc = proc_run(["pgrep", "-f", r"skillery.*daemon run"], timeout=30)
    except Exception:
        return []
    out = proc.stdout or ""
    return [
        int(line.strip())
        for line in out.splitlines()
        if line.strip().isdigit() and int(line.strip()) != os.getpid()
    ]


def find_daemon_pids() -> list[int]:
    """Все живые процессы демона — включая «осиротевшие» от прежних версий.

    Нужно для зачистки: PID-файл знает лишь про последнего запущенного, а на
    машине могли накопиться копии, поднятые до появления лока.
    """
    return _windows_daemon_pids() if sys.platform == "win32" else _posix_daemon_pids()
