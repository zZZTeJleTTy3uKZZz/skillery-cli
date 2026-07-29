"""``skillery system config …`` — конфигурация хаба (#1224).

Матрица числила три ручки system-config за CLI, но обращений к ним в коде не
было. Заодно матрица врёт про путь: у backend это ``/system/config`` (через
СЛЭШ, ``routes/system_config.py``), а не ``/system-config``.

Глаголы: ``system config list`` / ``show`` / ``set``.

Права: чтение — ``hub.admin`` либо мягкое ``system.config.read``; запись —
только ``hub.admin``.

Секреты: у записей с ``is_secret=true`` backend отдаёт ``value=null`` — CLI
показывает их как ``(секрет скрыт)`` и НИКОГДА не печатает значение (его
попросту нет в ответе). Записанное значение тоже не эхоится обратно.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data

console = Console()

#: Плейсхолдер вместо значения секретной записи в text-режиме.
SECRET_MASK = "(секрет скрыт)"


def _display_value(entry: dict[str, Any]) -> str:
    """Значение записи для человека: секрет — маской, пусто — прочерком."""
    if entry.get("is_secret"):
        return SECRET_MASK
    value = entry.get("value")
    if value is None:
        return "—"
    return str(value)


def cmd_config_list() -> None:
    """Все конфиг-записи хаба (GET /system/config)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_system_config()
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            items = payload.get("items") or []
            if not items:
                console.print("[yellow]Конфигурация пуста[/]")
                return
            table = Table(title=f"Конфигурация хаба (всего: {len(items)})")
            table.add_column("key")
            table.add_column("тип")
            table.add_column("значение", overflow="fold")
            table.add_column("по умолчанию", overflow="fold")
            table.add_column("описание", overflow="fold")
            for entry in items:
                table.add_row(
                    str(entry.get("key") or "—"),
                    str(entry.get("value_type") or "—"),
                    _display_value(entry),
                    str(entry.get("default") or "—"),
                    str(entry.get("label") or entry.get("description") or "—"),
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_config_show(
    key: str = typer.Argument(..., help="Ключ конфигурации"),
) -> None:
    """Одна конфиг-запись (GET /system/config/{key})."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_system_config(key)
        finally:
            await client.close()

        def _render(entry: dict[str, Any]) -> None:
            console.print(f"[bold]{entry.get('key')}[/]")
            console.print(f"  Значение: {_display_value(entry)}")
            console.print(f"  Тип: {entry.get('value_type') or '—'}")
            console.print(f"  По умолчанию: {entry.get('default') or '—'}")
            console.print(f"  Описание: {entry.get('description') or entry.get('label') or '—'}")
            allowed = entry.get("allowed_values")
            if allowed:
                console.print(f"  Допустимые: {', '.join(str(a) for a in allowed)}")
            console.print(f"  Обновлено: {entry.get('updated_at') or '—'}")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_config_set(
    key: str = typer.Argument(..., help="Ключ конфигурации"),
    value: str = typer.Argument(
        ...,
        help=(
            "Значение СТРОКОЙ — backend сам разберёт его по типу записи "
            "(int/bool/json)"
        ),
    ),
) -> None:
    """Изменить значение конфига (PATCH /system/config/{key}). Только hub.admin.

    Значение всегда передаётся строкой: у backend один контракт на все типы,
    а разбор идёт по ``value_type`` самой записи.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.set_system_config(key, value=value)
        finally:
            await client.close()

        def _render(entry: dict[str, Any]) -> None:
            # Не эхоим введённое значение: запись может быть секретной, и в
            # логах терминала ему делать нечего. Показываем то, что вернул
            # backend (у секретов это null → маска).
            console.print(
                f"[green]✓[/] {entry.get('key')} = {_display_value(entry)}"
            )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer, *, can_manage: bool = False) -> None:
    """Регистрирует ``system`` с вложенной группой ``config``.

    Ресурс — ``system``, а не ``config``: плоское имя ``config`` уже занято
    настройками САМОГО CLI (``skillery cli config``), и две разные вещи под
    одним словом путали бы в справке.
    """
    if not can_manage:
        return
    system_app = typer.Typer(
        no_args_is_help=True, help="Хаб как система: конфигурация."
    )
    config_app = typer.Typer(
        no_args_is_help=True,
        help="Конфигурация хаба (hub.admin).",
    )
    config_app.command("list")(cmd_config_list)
    config_app.command("show")(cmd_config_show)
    config_app.command("set")(cmd_config_set)
    system_app.add_typer(config_app, name="config")
    app.add_typer(system_app, name="system")
