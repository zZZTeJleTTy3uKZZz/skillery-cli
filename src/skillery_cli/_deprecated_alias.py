"""Deprecation-shim старой команды ``skills-hub`` → ``skillery``.

Ребренд (#283): актуальная команда — ``skillery``. Старое имя ``skills-hub``
больше НЕ выполняет CLI, а печатает подсказку и завершается с кодом 2, чтобы
пользователь перешёл на новую команду. Зарегистрировано как отдельный
entry-point в pyproject ([project.scripts] skills-hub).
"""
from __future__ import annotations

import sys

from skillery_cli import _branding


def main() -> None:  # noqa: D401 — простой shim
    args = " ".join(sys.argv[1:]).strip()
    suffix = f" {args}" if args else " --help"
    app = _branding.APP_NAME
    msg = (
        f"\033[33m⚠ Команда «skills-hub» переименована в «{app}».\033[0m\n"
        f"  Используйте: \033[1m{app}{suffix}\033[0m\n"
        f"  Подсказка: {app} --help\n"
    )
    sys.stderr.write(msg)
    raise SystemExit(2)
