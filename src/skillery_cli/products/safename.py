"""Простые имена в install-plan и в ``products.json`` — как ``safeJoin`` в Go.

``key`` компонента, ``install.bin``, ``install.name`` и ``skill_ref``
становятся ИМЕНЕМ каталога или файла внутри своего корня (bin, стор навыков,
каталоги плагинов). План приходит с backend, а ``products.json`` — общий файл
с Go-CLI, поэтому имя из него такие же недоверенные данные, как имя из плана:
проводить его в ``Path / name`` без проверки значит выпустить установку и
снятие за пределы корня.

Правило одно на оба CLI (``skillery-go/internal/install/safename.go``):
буквы, цифры, ``.``, ``_``, ``-``, и это не ``.`` и не ``..``. Второй пояс —
проверка, что склейка осталась внутри корня: на случай неожиданностей ``Path``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .handlers.base import ProductError

#: Одна компонента пути: буквы, цифры, точка, подчёркивание, дефис.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_simple_name(field: str, name: str) -> str:
    """Проверить, что ``name`` — одна компонента пути; вернуть его же."""
    if name in (".", ".."):
        raise ProductError(f"{field}: имя {name!r} недопустимо: ссылка на каталог")
    if not _SAFE_NAME_RE.match(name):
        raise ProductError(
            f"{field}: имя {name!r} должно быть простым ([A-Za-z0-9._-]+)"
        )
    return name


def safe_join(root: Path, name: str, *, field: str = "путь") -> Path:
    """Склеить корень с простым именем и проверить, что итог внутри корня."""
    validate_simple_name(field, name)
    joined = root / name
    if not path_within(root, joined):
        raise ProductError(f"путь {str(joined)!r} выходит за каталог {str(root)!r}")
    return joined


def path_within(root: Path, target: Path) -> bool:
    """Лежит ли ``target`` внутри ``root`` (``root`` включительно)."""
    abs_root = os.path.abspath(root)
    abs_target = os.path.abspath(target)
    try:
        relative = os.path.relpath(abs_target, abs_root)
    except ValueError:  # разные диски на Windows
        return False
    if os.path.isabs(relative):
        return False
    return relative != ".." and not relative.startswith(".." + os.sep)


__all__ = ["path_within", "safe_join", "validate_simple_name"]
