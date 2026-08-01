"""Унифицированный вывод text|json — тонкая обёртка над ``clikit.output``.

cli-kits W5: бэкенд вывода — ``clikit.output`` (emit_data/emit_message/
emit_error/init_output_mode). Здесь сохраняется ИСТОРИЧЕСКИЙ публичный API
этого модуля (``emit_data`` / ``emit_message`` / ``emit_error`` / ``is_json`` /
``init_output_mode`` / ``console``), чтобы НИ ОДИН потребитель
(``commands/*``, ``__main__``) не пришлось менять.

Отличия от clikit-дефолтов, которые мы сознательно сохраняем:

* **Дефолт — text.** ``clikit.output`` по умолчанию выдаёт json (для
  AI-агентов). Здесь исторический контракт: без флага/конфига режим — text,
  ``--json`` переключает (через ``init_output_mode(json_flag=...)``).
* **Источник правды режима — локальный ``_mode``.** Тесты (и историческая
  логика) мутируют ``output._mode`` напрямую; перед каждой делегацией режим
  зеркалится в ``clikit.output`` (``_sync_clikit_mode``), чтобы делегаты вели
  себя согласованно. Брендовый env-ключ — ``SKILLERY_OUTPUT``.

JSON-режим: результат команды (``emit_data``) — в stdout строго JSON Lines;
сообщения/ошибки (``emit_message``/``emit_error``) — в stderr (stdout остаётся
чистым машинным каналом).
"""
from __future__ import annotations

import os
from typing import Any

import clikit.output as _clikit
from rich.console import Console

from skillery_cli import _branding

_BRAND = _branding.ENV_PREFIX  # env-ключ режима: <PREFIX>_OUTPUT

# Источник правды режима для ЭТОГО модуля. Тесты мутируют его напрямую
# (`output._mode = "json"`), поэтому держим локально и зеркалим в clikit.
_mode: str = "text"  # исторический дефолт — text (clikit по умолчанию json)


def _sync_clikit_mode() -> None:
    """Зеркалит локальный ``_mode`` в ``clikit.output`` перед делегацией.

    ⚠️ В clikit >=0.1.7 ``_mode`` — ``ContextVar``, а не строка. Присваивание
    ``_clikit._mode = _mode`` подменяло сам ContextVar строкой, после чего
    ``is_json()`` падал с ``AttributeError: 'str' object has no attribute
    'get'`` — то есть ЛЮБАЯ команда CLI переставала работать. Поймано на
    свежей установке 0.5.79, где подтянулся clikit 0.1.7.

    Поэтому: ContextVar обновляем через ``.set()``, а голому атрибуту
    (старые clikit) присваиваем как раньше — обратная совместимость.
    """
    target = getattr(_clikit, "_mode", None)
    if hasattr(target, "set"):
        target.set(_mode)
    else:
        _clikit._mode = _mode


def init_output_mode(*, json_flag: bool, config_format: str) -> None:
    """Приоритет: --json > env SKILLERY_OUTPUT > config.output_format > text.

    Сигнатура сохранена исторической (keyword-only ``json_flag`` /
    ``config_format``) — ``__main__`` зовёт её именно так. Дефолт — text
    (передаём ``json_flag`` как явный text-сигнал в clikit, когда он False,
    но финальную истину держим локально).
    """
    global _mode
    env_mode = os.environ.get(f"{_BRAND}_OUTPUT", "").strip().lower()
    if json_flag:
        _mode = "json"
    elif env_mode in ("json", "text"):
        _mode = env_mode
    elif config_format in ("json", "text"):
        _mode = config_format
    else:
        _mode = "text"
    _sync_clikit_mode()


def is_json() -> bool:
    return _mode == "json"


def console() -> Console:
    """Rich-консоль для text-режима. В json-режиме не используется."""
    return _clikit.console()


def emit_data(payload: dict | list | Any, *, text_renderer=None) -> None:  # noqa: ANN001
    """Основной вывод команды → ``clikit.output.emit_data``.

    json-режим: ``json.dumps(payload)`` в stdout (JSON Lines).
    text-режим: ``text_renderer(payload)`` если передан, иначе pretty-JSON.
    """
    _sync_clikit_mode()
    _clikit.emit_data(payload, text_renderer=text_renderer)


def emit_message(text: str, *, level: str = "info", **extra: Any) -> None:
    """Информационное сообщение (НЕ результат) → ``clikit.output.emit_message``.

    json-режим: ``{"event": level, "message": text, ...extra}`` в stderr.
    text-режим: цветной print (info=✓, warn=!, error=✗).
    """
    _sync_clikit_mode()
    _clikit.emit_message(text, level=level, **extra)


def emit_error(code: str, message: str, **extra: Any) -> None:
    """Ошибка → ``clikit.output.emit_error`` (exit-нонзеро — отдельно).

    Историческая сигнатура ``emit_error(code, message, **extra)`` сохранена.
    ``status_code`` (если передан в ``extra``) пробрасывается явным kwarg'ом —
    clikit принимает его keyword-only.
    """
    _sync_clikit_mode()
    status_code = extra.pop("status_code", None)
    _clikit.emit_error(code, message, status_code=status_code, **extra)
