"""Локальный стор CLI-энтрипоинтов навыков + кроссплатформенный PATH (E3).

Навык типа ``tooling`` несёт ``[[cli]]`` (command_name + entrypoint, E6). Чтобы
команда установленного навыка звалась из ЛЮБОЙ директории, мы кладём
исполняемый **shim** в bin-каталог стора (``~/.skills-hub/bin``) и единожды
добавляем этот каталог в PATH пользователя.

Модель (симметрично linker/installer):
- ``bin_dir()`` — каталог шимов: ``effective_store_dir().parent / 'bin'``
  (env-override ``SKILLS_HUB_BIN_DIR`` для тестов/нестандартных раскладок);
- ``add_cli(command_name, entrypoint, *, skill_slug)`` — кладёт shim:
    * **Windows** → ``<command>.cmd`` батник (вызывает entrypoint + ``%*``).
      Если entrypoint — путь к ``.exe`` (console-script venv), батник зовёт его
      напрямую в кавычках; иначе — как команду (``python -m pkg``);
    * **POSIX** → если entrypoint — существующий исполняемый файл, ставим
      symlink; иначе пишем shebang-обёртку (``#!/bin/sh`` + ``exec ... "$@"``)
      и ``chmod +x``.
  Рядом пишется sidecar ``<command>.json`` с привязкой к навыку (для
  ``list_clis`` и снятия при disable/remove);
- ``remove_cli(command_name)`` — удаляет shim + sidecar (disable/remove навыка);
- ``ensure_on_path()`` — idempotent: bin в PATH? нет → добавить без admin
  (Windows: User PATH через ``HKCU\\Environment``; POSIX: ``export PATH`` строкой
  в rc-файл). Возвращает ``{"status": added|already|manual-needed, ...}``;
- ``list_clis()`` — какие CLI установлены (из sidecar'ов).

КРИТИЧНО (кроссплатформа): ветка ОС выбирается по модульной константе
``IS_WINDOWS`` (тесты её мокают), а сами IO-примитивы PATH вынесены в
подменяемые функции — чтобы покрыть win/mac/linux на любом хосте.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Sidecar с метаданными шима (привязка к навыку + как он построен).
_SIDECAR_SUFFIX = ".json"

# Маркер строки, которую мы дописываем в POSIX rc — для idempotent-проверки.
_RC_MARKER = "# added by skills-hub (CLI store on PATH)"


# --------------------------------------------------------------------------
#  bin_dir
# --------------------------------------------------------------------------
def bin_dir() -> Path:
    """Каталог шимов CLI. env ``SKILLS_HUB_BIN_DIR`` > ``<store>/../bin``."""
    override = os.environ.get("SKILLS_HUB_BIN_DIR")
    if override:
        return Path(override).expanduser()
    # Лениво, чтобы не тянуть config на уровне модуля (симметрия installer).
    from skills_hub_cli.config import _default_store_dir

    return _default_store_dir().parent / "bin"


def _sidecar_path(command_name: str) -> Path:
    return bin_dir() / f"{command_name}{_SIDECAR_SUFFIX}"


def _shim_path(command_name: str) -> Path:
    """Путь к самому шиму (без sidecar) с учётом ОС-расширения."""
    return bin_dir() / (f"{command_name}.cmd" if IS_WINDOWS else command_name)


# --------------------------------------------------------------------------
#  add_cli
# --------------------------------------------------------------------------
def _looks_like_executable_path(entrypoint: str) -> Path | None:
    """Если entrypoint — путь к СУЩЕСТВУЮЩЕМУ файлу, вернуть его Path, иначе None.

    Console-script навыка может ставиться как venv-exe (Windows) или
    исполняемый файл (POSIX). Команды вида ``python -m pkg`` / ``pkg:func``
    путём не являются (содержат пробел/двоеточие-спека) → None.
    """
    candidate = entrypoint.strip()
    if not candidate or " " in candidate:
        return None
    p = Path(candidate).expanduser()
    return p if p.exists() else None


def _write_windows_shim(shim: Path, entrypoint: str) -> None:
    """``.cmd`` батник: вызывает entrypoint и прокидывает все аргументы (%*)."""
    exe = _looks_like_executable_path(entrypoint)
    invocation = f'"{exe}"' if exe is not None else entrypoint
    body = f"@echo off\r\n{invocation} %*\r\n"
    shim.write_text(body, encoding="utf-8")


def _write_posix_shim(shim: Path, entrypoint: str) -> bool:
    """POSIX shim. Возвращает True если поставлен symlink, False если скрипт.

    - entrypoint — существующий исполняемый файл → symlink (дешевле и честнее);
    - иначе → shebang-обёртка ``#!/bin/sh`` + ``exec <entrypoint> "$@"`` + chmod.
    """
    exe = _looks_like_executable_path(entrypoint)
    if exe is not None and os.access(exe, os.X_OK):
        with contextlib.suppress(FileNotFoundError):
            shim.unlink()
        os.symlink(exe, shim)
        return True
    body = f'#!/bin/sh\nexec {entrypoint} "$@"\n'
    shim.write_text(body, encoding="utf-8")
    _chmod_exec(shim)
    return False


def _chmod_exec(path: Path) -> None:
    """chmod +x (user/group/other), best-effort."""
    with contextlib.suppress(OSError):
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def add_cli(command_name: str, entrypoint: str, *, skill_slug: str) -> Path:
    """Положить исполняемый shim ``command_name`` в bin-каталог стора.

    ``entrypoint`` — что выполнять: команда (``python -m pkg.cli``) ИЛИ путь к
    исполняемому console-script. Идемпотентно: повторный вызов перезаписывает
    shim и sidecar. Возвращает путь к созданному шиму.
    """
    if not command_name or not command_name.strip():
        raise ValueError("command_name не может быть пустым")
    if not entrypoint or not entrypoint.strip():
        raise ValueError("entrypoint не может быть пустым")

    d = bin_dir()
    d.mkdir(parents=True, exist_ok=True)
    shim = _shim_path(command_name)

    # Снимаем прежний shim (любого вида) перед перезаписью — idempotent.
    _unlink_shim(shim)

    kind = "cmd"
    if IS_WINDOWS:
        _write_windows_shim(shim, entrypoint)
    else:
        kind = "symlink" if _write_posix_shim(shim, entrypoint) else "script"

    _sidecar_path(command_name).write_text(
        json.dumps(
            {
                "command_name": command_name,
                "entrypoint": entrypoint,
                "skill_slug": skill_slug,
                "kind": kind,
                "shim": str(shim),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return shim


def _unlink_shim(shim: Path) -> None:
    """Удалить файл/symlink шима, не падая если его нет."""
    with contextlib.suppress(FileNotFoundError):
        if shim.is_symlink() or shim.exists():
            shim.unlink()


# --------------------------------------------------------------------------
#  remove_cli
# --------------------------------------------------------------------------
def remove_cli(command_name: str) -> bool:
    """Снять shim (+ sidecar) команды. True если что-то удалили, иначе False."""
    shim = _shim_path(command_name)
    sidecar = _sidecar_path(command_name)
    removed = False
    if shim.is_symlink() or shim.exists():
        _unlink_shim(shim)
        removed = True
    if sidecar.exists():
        sidecar.unlink()
        removed = True
    return removed


# --------------------------------------------------------------------------
#  list_clis
# --------------------------------------------------------------------------
def list_clis() -> list[dict[str, str]]:
    """Перечень установленных CLI (из sidecar'ов), отсортирован по команде."""
    d = bin_dir()
    if not d.exists():
        return []
    out: list[dict[str, str]] = []
    for sc in sorted(d.glob(f"*{_SIDECAR_SUFFIX}")):
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("command_name"):
            out.append(data)
    out.sort(key=lambda c: c.get("command_name", ""))
    return out


# --------------------------------------------------------------------------
#  ensure_on_path
# --------------------------------------------------------------------------
def _path_entries() -> list[str]:
    return [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def _already_on_path(target: Path) -> bool:
    """target уже в PATH процесса (нормализованное сравнение)."""
    want = os.path.normcase(os.path.normpath(str(target)))
    return any(
        os.path.normcase(os.path.normpath(p)) == want for p in _path_entries()
    )


def ensure_on_path() -> dict[str, str]:
    """Гарантировать bin-каталог стора в PATH (idempotent, без admin).

    Возвращает dict со ``status``:
    - ``already``       — каталог уже в PATH (ничего не делали);
    - ``added``         — добавили (Windows: User PATH; POSIX: строка в rc);
    - ``manual-needed`` — не смогли записать автоматически, в ``instruction``
      лежит готовая команда для пользователя.
    Всегда содержит ключ ``bin_dir``.
    """
    target = bin_dir()
    target.mkdir(parents=True, exist_ok=True)
    base = {"bin_dir": str(target)}

    if IS_WINDOWS:
        return {**base, **_ensure_on_path_windows(target)}
    return {**base, **_ensure_on_path_posix(target)}


# ---- Windows (User PATH через HKCU\Environment, без admin) ----------------
def _win_read_user_path() -> str:
    """Текущий User PATH (HKCU\\Environment\\Path). '' если ключа нет."""
    try:
        import winreg
    except ImportError:  # не-Windows хост — не должно вызываться
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "Path")
            return str(value)
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def _win_write_user_path(value: str) -> None:
    """Записать User PATH (REG_EXPAND_SZ) + broadcast WM_SETTINGCHANGE."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)
    _win_broadcast_env_change()


def _win_broadcast_env_change() -> None:
    """Сообщить системе об изменении окружения (новые процессы увидят PATH)."""
    with contextlib.suppress(Exception):
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, None,
        )


def _ensure_on_path_windows(target: Path) -> dict[str, str]:
    if _already_on_path(target):
        return {"status": "already"}
    current = _win_read_user_path()
    # уже в User PATH (но не в PATH процесса — applied для будущих сессий)?
    want = os.path.normcase(os.path.normpath(str(target)))
    parts = [p for p in current.split(";") if p]
    if any(os.path.normcase(os.path.normpath(p)) == want for p in parts):
        return {"status": "already"}
    new_value = (current + ";" + str(target)) if current else str(target)
    try:
        _win_write_user_path(new_value)
    except OSError as exc:
        return {
            "status": "manual-needed",
            "instruction": (
                f'setx PATH "%PATH%;{target}"  (или добавьте {target} в '
                "переменную среды Path пользователя вручную)"
            ),
            "error": str(exc),
        }
    return {"status": "added"}


# ---- POSIX (export PATH строкой в rc-файл) --------------------------------
def _posix_rc_file() -> Path:
    """Подходящий rc-файл для текущей оболочки.

    Предпочтение: для zsh → ``~/.zshrc``; иначе ``~/.bashrc`` если существует;
    fallback — ``~/.profile`` (читается логин-шеллами по умолчанию). Файл
    создаётся пустым, если его ещё нет.
    """
    home = Path.home()
    shell = os.environ.get("SHELL", "")
    candidates: list[Path] = []
    if shell.endswith("zsh"):
        candidates.append(home / ".zshrc")
    elif shell.endswith("bash"):
        candidates.append(home / ".bashrc")
    candidates.append(home / ".profile")

    for rc in candidates:
        if rc.exists():
            return rc
    # Ни один не существует — создаём первый предпочтительный.
    rc = candidates[0]
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.touch()
    return rc


def _export_line(target: Path) -> str:
    return f'export PATH="{target}:$PATH"'


def _ensure_on_path_posix(target: Path) -> dict[str, str]:
    if _already_on_path(target):
        return {"status": "already"}

    export = _export_line(target)
    try:
        rc = _posix_rc_file()
    except OSError as exc:
        return {
            "status": "manual-needed",
            "instruction": export,
            "error": str(exc),
        }

    try:
        existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
        # idempotent: уже добавляли этот каталог (по export-строке)?
        if str(target) in existing and "export PATH" in existing:
            return {"status": "already"}
        sep = "" if existing.endswith("\n") or existing == "" else "\n"
        rc.write_text(
            f"{existing}{sep}{_RC_MARKER}\n{export}\n", encoding="utf-8"
        )
    except OSError as exc:
        return {
            "status": "manual-needed",
            "instruction": export,
            "error": str(exc),
        }
    return {"status": "added", "rc_file": str(rc)}
