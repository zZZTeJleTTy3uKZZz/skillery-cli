"""Единственный источник брендовых строк CLI.

Ребренд дистрибутива = правка ТОЛЬКО этого файла. Нигде больше имя бренда в
коде фигурировать не должно — ни в идентификаторах (имена функций/классов
brand-agnostic), ни разбросанными литералами. Всё, что «знает» имя продукта,
берётся отсюда.

Замечание: доменные термины (``hub`` — маркетплейс навыков, ``hub.admin`` —
permission-ключ из backend) НЕ являются брендом дистрибутива и остаются как
есть — они переживают любое переименование продукта.
"""
from __future__ import annotations

APP_NAME = "skillery"
"""Имя приложения / CLI-команды (project.scripts entry-point)."""

DIST_NAME = "skillery-cli"
"""Имя дистрибутива на PyPI; оно же — keyring-сервис."""

ENV_PREFIX = "SKILLERY"
"""Префикс env-переменных: ``f"{ENV_PREFIX}_CONFIG_DIR"`` и т.п."""

HOME_DIR_NAME = f".{APP_NAME}"
"""Дефолтный home-каталог конфига/стора: ``~/.skillery``."""

DAEMON_SERVICE_ID = f"com.{APP_NAME}.daemon"
"""launchd label / общий идентификатор сервиса демона."""

DAEMON_UNIT_BASENAME = f"{APP_NAME}-daemon"
"""Базовое имя unit-файлов демона (``.service`` / ``.xml``)."""

DAEMON_TASK_NAME = f"{APP_NAME.capitalize()}Daemon"
"""Имя задачи Windows Task Scheduler."""


def env(suffix: str) -> str:
    """Полное имя env-переменной по суффиксу: ``env("CONFIG_DIR")`` → ``SKILLERY_CONFIG_DIR``."""
    return f"{ENV_PREFIX}_{suffix}"
