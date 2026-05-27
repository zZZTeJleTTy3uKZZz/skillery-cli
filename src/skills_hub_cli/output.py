"""Унифицированный вывод text|json.

Глобальный state выставляется при старте CLI (pre-pass argv для --json
до построения typer-app, аналогично --profile). Команды используют
функции `emit_data` / `emit_message` / `emit_error` чтобы не дублировать
"если json — JSON, иначе rich".

JSON режим — для AI агентов и скриптинга. Все эмиты идут в stdout
строго в формате JSON Lines (одна JSON-структура на вывод команды).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from rich.console import Console

_console = Console()
_mode: str = "text"  # глобальный — устанавливается init_output_mode()


def init_output_mode(*, json_flag: bool, config_format: str) -> None:
    """Приоритет: --json > env SKILLS_HUB_OUTPUT > config.output_format > text."""
    global _mode
    env_mode = os.environ.get("SKILLS_HUB_OUTPUT", "").strip().lower()
    if json_flag:
        _mode = "json"
    elif env_mode in ("json", "text"):
        _mode = env_mode
    elif config_format in ("json", "text"):
        _mode = config_format
    else:
        _mode = "text"


def is_json() -> bool:
    return _mode == "json"


def console() -> Console:
    """Rich console для text-режима. В json-режиме не используется."""
    return _console


def emit_data(payload: dict | list | Any, *, text_renderer=None) -> None:  # noqa: ANN001
    """Основной вывод команды.

    json-режим: `json.dumps(payload)` в stdout.
    text-режим: вызывает `text_renderer(payload)` (обычно Rich-таблица).
    Если text_renderer не передан — pretty-print JSON.
    """
    if is_json():
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return
    if text_renderer is not None:
        text_renderer(payload)
    else:
        from rich.json import JSON as RichJSON

        _console.print(RichJSON.from_data(payload))


def emit_message(text: str, *, level: str = "info", **extra: Any) -> None:
    """Информационное сообщение (НЕ результат команды).

    json-режим: {"event": level, "message": text, ...extra} в stderr.
    text-режим: цветной print в stdout (info=зелёный ✓, warn=жёлтый, error=красный).
    """
    if is_json():
        rec = {"event": level, "message": text, **extra}
        print(json.dumps(rec, ensure_ascii=False, default=str), file=sys.stderr)
        return
    icons = {"info": "[green]✓[/]", "warn": "[yellow]![/]", "error": "[red]✗[/]"}
    icon = icons.get(level, "")
    _console.print(f"{icon} {text}")


def emit_error(code: str, message: str, **extra: Any) -> None:
    """Ошибка — exit-нонзеро вызывается отдельно."""
    if is_json():
        rec = {"event": "error", "code": code, "message": message, **extra}
        print(json.dumps(rec, ensure_ascii=False, default=str), file=sys.stderr)
    else:
        _console.print(f"[red]{code}:[/] {message}")
