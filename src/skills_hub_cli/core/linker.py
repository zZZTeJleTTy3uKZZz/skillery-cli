"""Кросс-платформенная линковка skill-каталогов: junction (Windows) / symlink (POSIX).

Стор-каталог (`~/.skills-hub/store/<dir>/`) линкуется в scope агента
(`~/.claude/skills/<dir>` или `<project>/.claude/skills/<dir>`). На Windows
используется junction (reparse-point типа mount-point) — он НЕ требует прав
администратора / Developer Mode, в отличие от symlink. На POSIX — symlink на
директорию.

КРИТИЧНО:
- `os.path.islink()` для junction возвращает False → детект через reparse-tag.
- `remove_link` удаляет ТОЛЬКО ссылку (rmdir/unlink), НИКОГДА не rmtree —
  иначе на Windows можно уйти внутрь target и снести содержимое стора.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Тип результата create_link: "junction" | "symlink".
LinkKind = str


def is_link(path: Path) -> bool:
    """True если path — symlink (любая ОС) ИЛИ junction (Windows reparse-point)."""
    path = Path(path)
    if os.path.islink(path):
        return True
    if IS_WINDOWS:
        try:
            st = os.lstat(path)
        except OSError:
            return False
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def link_target(path: Path) -> Path | None:
    """Абсолютный нормализованный путь, куда указывает ссылка, либо None."""
    if not is_link(path):
        return None
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    # Windows junction может вернуть префикс \\?\ (extended-length path).
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    p = Path(raw)
    if not p.is_absolute():
        p = Path(path).parent / p
    return Path(os.path.normpath(os.path.abspath(p)))


def _create_junction(link: Path, target: Path) -> None:
    """Windows junction: пробуем _winapi.CreateJunction, fallback `mklink /J`."""
    try:
        import _winapi

        # Сигнатура: CreateJunction(src_path=target, dst_path=link).
        _winapi.CreateJunction(str(target), str(link))
        return
    except (ImportError, AttributeError, OSError):
        pass
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )


def create_link(link_path: Path, target_dir: Path) -> LinkKind:
    """Создаёт ссылку link_path → target_dir. Идемпотентно.

    - Если link_path уже ссылка на target_dir → no-op.
    - Если ссылка на другое → пересоздаётся.
    - Если link_path — обычная папка/файл → НЕ удаляется здесь (это решает
      вызывающий код, напр. installer с --force); бросаем FileExistsError.
    Бросает OSError если ОС/ФС не дала создать ссылку (вызывающий делает copy-fallback).
    """
    link_path = Path(link_path)
    target_dir = Path(os.path.abspath(target_dir))

    if is_link(link_path):
        existing = link_target(link_path)
        if existing is not None and os.path.normcase(str(existing)) == os.path.normcase(
            str(target_dir)
        ):
            return "junction" if IS_WINDOWS else "symlink"
        remove_link(link_path)
    elif link_path.exists():
        raise FileExistsError(f"{link_path} существует и не является ссылкой")

    link_path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        _create_junction(link_path, target_dir)
        return "junction"
    os.symlink(target_dir, link_path, target_is_directory=True)
    return "symlink"


def remove_link(path: Path) -> bool:
    """Удаляет ТОЛЬКО ссылку, не трогая target. True если удалили ссылку.

    Никогда не делает rmtree. Если path не ссылка — no-op, возвращает False.
    """
    path = Path(path)
    if not is_link(path):
        return False
    if IS_WINDOWS:
        # junction и dir-symlink удаляются rmdir; редкий file-symlink — remove.
        try:
            os.rmdir(path)
        except OSError:
            os.remove(path)
    else:
        os.unlink(path)
    return True
