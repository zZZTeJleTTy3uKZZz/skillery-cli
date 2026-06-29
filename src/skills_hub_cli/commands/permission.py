"""``skills-hub permissions`` / ``skills-hub role`` — D-CLI M-4.

Управление каталогом прав и набором прав роли (паритет с backend
``routes/permissions.py``). Гейт — ``hub.admin`` (в ``register`` через
``can_manage``; ``set-permissions`` бэкенд также допускает ``role.manage`` в
своей компании, но CLI-видимость держим консервативно на hub.admin —
backend всё равно финально режет 403).

Команды:
- ``permissions list [--q]`` — каталог всех прав (``GET /permissions``;
  любой авторизованный, но команда регистрируется под hub.admin вместе с
  ``role`` sub-app).
- ``role show <role_id>`` — права, привязанные к роли
  (``GET /roles/{id}/permissions``).
- ``role set-permissions <role_id> --perms k1,k2`` — replace-set прав роли
  по slug'ам (``PUT /roles/{id}/permissions``); ``--perms ""`` снимает все
  права.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.output import emit_data

console = Console()


def cmd_permissions_list(
    q: str | None = typer.Option(
        None, "--q", help="Поиск по slug/label/описанию (подстрока)"
    ),
) -> None:
    """Каталог всех прав платформы (GET /permissions).

    Ответ — канон-обёртка ``{items,total,page,size}``; CLI печатает весь
    каталог (backend default size=0 = без пагинации).
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_permissions(q=q)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            items = p.get("items") or []
            total = p.get("total") or len(items)
            if not items:
                console.print("[yellow]Прав не найдено[/]")
                return
            table = Table(title=f"Permissions (всего: {total})")
            table.add_column("key")
            table.add_column("label", overflow="fold")
            table.add_column("scope")
            table.add_column("используется ролями")
            for perm in items:
                table.add_row(
                    perm.get("key") or perm.get("slug") or "—",
                    perm.get("label") or perm.get("description") or "—",
                    str(perm.get("scope") or "—"),
                    str(perm.get("used_by_roles_count", 0)),
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_role_show(
    role_id: str = typer.Argument(..., help="ID роли (см. `skillery roles`)"),
) -> None:
    """Права, привязанные к роли (GET /roles/{id}/permissions).

    Ответ — плоский список ``PermissionDTO``.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            perms = await client.list_role_permissions(role_id)
        finally:
            await client.close()

        def _render(items: list[dict[str, Any]]) -> None:
            if not items:
                console.print(
                    f"[yellow]У роли {role_id} нет прав[/]"
                )
                return
            table = Table(title=f"Права роли {role_id} (всего: {len(items)})")
            table.add_column("key")
            table.add_column("label", overflow="fold")
            table.add_column("scope")
            for perm in items:
                table.add_row(
                    perm.get("key") or perm.get("slug") or "—",
                    perm.get("label") or perm.get("description") or "—",
                    str(perm.get("scope") or "—"),
                )
            console.print(table)

        emit_data(perms, text_renderer=_render)

    _common.run(_do())


def cmd_role_set_permissions(
    role_id: str = typer.Argument(..., help="ID роли"),
    perms: str = typer.Option(
        ...,
        "--perms",
        help=(
            "Ключи прав через запятую (replace-set; пусто = снять все). "
            "Напр.: skill.publish,skill.manage,tag.create"
        ),
    ),
) -> None:
    """Заменить набор прав роли (PUT /roles/{id}/permissions).

    Replace-set: полная замена набора по slug'ам прав. ``--perms ""`` снимает
    все права. Право hub.admin (или role.manage в своей компании — backend
    финально режет 403/422). Бэкенд инвалидирует кэш прав роли — изменения
    применяются сразу.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    slugs = [s.strip() for s in perms.split(",") if s.strip()]

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            result = await client.set_role_permissions(
                role_id, permission_slugs=slugs
            )
        finally:
            await client.close()

        def _render(items: list[dict[str, Any]]) -> None:
            keys = [
                (perm.get("key") or perm.get("slug") or "—") for perm in items
            ]
            console.print(
                f"[green]✓[/] Права роли {role_id} обновлены "
                f"({len(keys)} прав)"
            )
            console.print(f"  Набор: {', '.join(keys) or '(пусто)'}")

        emit_data(result, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer, *, can_manage: bool = False) -> None:
    """Регистрирует ``permissions`` + sub-app ``role`` (M-4).

    Гейт ``can_manage`` (hub.admin). Без него команды не появляются —
    управление правами доступно только администратору хаба.
    """
    if not can_manage:
        return
    app.command(name="permissions")(cmd_permissions_list)
    role_app = typer.Typer(
        no_args_is_help=True,
        help="Права ролей: show / set-permissions (hub.admin)",
    )
    role_app.command("show")(cmd_role_show)
    role_app.command("set-permissions")(cmd_role_set_permissions)
    app.add_typer(role_app, name="role")
