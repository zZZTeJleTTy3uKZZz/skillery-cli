"""``skillery permissions`` / ``skillery role``.

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

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data

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


def cmd_role_get(
    role_id: str = typer.Argument(..., help="ID роли (см. `skillery role list`)"),
) -> None:
    """Карточка роли целиком (GET /roles/{id}) — #1224.

    Отличие от ``role show``: тот отдаёт ПРАВА роли
    (``GET /roles/{id}/permissions``), а это — саму роль: slug, флаги,
    количество носителей. Имя ``show`` не переиспользуем, чтобы не менять
    форму вывода у тех, кто уже парсит его в скриптах.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_role(role_id)
        finally:
            await client.close()

        def _render(role: dict[str, Any]) -> None:
            console.print(f"[bold]{role.get('name')}[/] ({role.get('slug')})")
            console.print(f"  ID: {role.get('id')}")
            console.print(f"  Описание: {role.get('description') or '—'}")
            console.print(f"  Системная: {'да' if role.get('is_system') else 'нет'}")
            console.print(f"  По умолчанию: {'да' if role.get('is_default') else 'нет'}")
            console.print(
                "  Назначается компанией: "
                f"{'да' if role.get('is_assignable_by_company') else 'нет'}"
            )
            console.print(f"  Носителей: {role.get('member_count', 0)}")
            keys = role.get("permission_keys") or []
            console.print(f"  Прав: {len(keys)}")
            if keys:
                console.print(f"    {', '.join(keys)}")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_role_create(
    slug: str = typer.Argument(..., help="Машинный slug роли, напр. auditor"),
    name: str = typer.Option(..., "--name", help="Человекочитаемое имя"),
    description: str | None = typer.Option(None, "--description", help="Описание"),
    perms: str | None = typer.Option(
        None, "--perms", help="Ключи прав через запятую (можно задать позже)"
    ),
    is_default: bool = typer.Option(
        False, "--default", help="Выдавать новым пользователям"
    ),
    assignable_by_company: bool = typer.Option(
        False, "--company-assignable", help="Компания может назначать эту роль"
    ),
) -> None:
    """Создать глобальную роль (POST /roles). Право hub.admin."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    keys = [p.strip() for p in (perms or "").split(",") if p.strip()]

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.create_role(
                slug=slug,
                name=name,
                permission_keys=keys,
                description=description,
                is_default=is_default,
                is_assignable_by_company=assignable_by_company,
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda r: console.print(
                f"[green]✓[/] Роль создана: {r.get('name')} "
                f"({r.get('slug')}, id={r.get('id')})"
            ),
        )

    _common.run(_do())


def cmd_role_edit(
    role_id: str = typer.Argument(..., help="ID роли"),
    name: str | None = typer.Option(None, "--name", help="Новое имя"),
    description: str | None = typer.Option(None, "--description", help="Описание"),
    perms: str | None = typer.Option(
        None,
        "--perms",
        help="Ключи прав через запятую — ПОЛНАЯ замена набора",
    ),
    is_default: bool | None = typer.Option(
        None, "--default/--no-default", help="Выдавать новым пользователям"
    ),
    assignable_by_company: bool | None = typer.Option(
        None,
        "--company-assignable/--no-company-assignable",
        help="Может ли компания назначать эту роль",
    ),
) -> None:
    """Обновить роль (PATCH /roles/{id}). Право hub.admin.

    ``slug`` неизменяем — backend его в запросе не принимает.
    """
    keys = (
        [p.strip() for p in perms.split(",") if p.strip()]
        if perms is not None
        else None
    )
    if (
        name is None
        and description is None
        and keys is None
        and is_default is None
        and assignable_by_company is None
    ):
        console.print("[yellow]Нечего менять:[/] задайте хотя бы одну опцию")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.update_role(
                role_id,
                name=name,
                description=description,
                permission_keys=keys,
                is_default=is_default,
                is_assignable_by_company=assignable_by_company,
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda r: console.print(
                f"[green]✓[/] Роль обновлена: {r.get('name')} (id={r.get('id')})"
            ),
        )

    _common.run(_do())


def cmd_role_delete(
    role_id: str = typer.Argument(..., help="ID роли"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения"),
) -> None:
    """Удалить роль (DELETE /roles/{id}). Право hub.admin."""
    if not yes:
        typer.confirm(f"Удалить роль {role_id}?", abort=True)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.delete_role(role_id)
        finally:
            await client.close()

        emit_data(
            {"deleted": True, "role_id": role_id},
            text_renderer=lambda _p: console.print(
                f"[green]✓[/] Роль {role_id} удалена"
            ),
        )

    _common.run(_do())


def cmd_role_assign(
    role_id: str = typer.Argument(..., help="ID роли"),
    user_id: str = typer.Option(..., "--user", help="ID пользователя"),
) -> None:
    """Назначить пользователю ГЛОБАЛЬНУЮ роль (POST /role-assignments).

    Идемпотентно по паре (пользователь, платформа): повторный вызов с другой
    ролью — это СМЕНА роли, а не второе назначение. Роль в конкретной
    компании назначается другой командой — ``skillery member change-role``.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.assign_role(user_id=user_id, role_id=role_id)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            # `created` приходит СТРОКОЙ "true"/"false" — сравниваем как строку.
            created = str(payload.get("created", "")).lower() == "true"
            verb = "назначена" if created else "уже была назначена (обновлена)"
            console.print(
                f"[green]✓[/] Роль {role_id} {verb} пользователю {user_id}"
            )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer, *, can_manage: bool = False) -> None:
    """Регистрирует ``permissions`` + sub-app ``role`` (M-4, расширен в #1224).

    Гейт ``can_manage`` (hub.admin). Без него команды не появляются —
    управление правами доступно только администратору хаба.
    """
    if not can_manage:
        return
    app.command(name="permissions")(cmd_permissions_list)
    role_app = typer.Typer(
        no_args_is_help=True,
        help="Роли: карточка, права, создание/правка/удаление, назначение (hub.admin)",
    )
    role_app.command("show")(cmd_role_show)
    role_app.command("set-permissions")(cmd_role_set_permissions)
    role_app.command("get")(cmd_role_get)
    role_app.command("create")(cmd_role_create)
    role_app.command("edit")(cmd_role_edit)
    role_app.command("delete")(cmd_role_delete)
    role_app.command("assign")(cmd_role_assign)
    app.add_typer(role_app, name="role")
