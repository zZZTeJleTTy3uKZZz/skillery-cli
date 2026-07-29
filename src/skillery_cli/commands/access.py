"""``skillery access …`` — гранты доступа к навыкам и коллекциям (#1224).

Матрица функционала числила все шесть access-grant ручек за CLI, но в коде
не было ни одного обращения к ``/access-grants``. Паритет с backend
``routes/access_grants.py``.

Ресурс один (``access``), а объект выбирается взаимоисключающими опциями
``--skill`` / ``--collection``: у обеих половин backend'а один и тот же
контракт гранта, и разводить их в две почти одинаковые группы значило бы
дублировать команды ради разной подстроки в URL.

Глаголы: ``access list`` / ``access grant`` / ``access revoke``.

Гранты «в обратную сторону» (``/users/{id}/access-grants``,
``/companies/{id}/access-grants`` — что выдано субъекту) сознательно НЕ
реализованы: у них нет потребителя и в вебе, в матрице они помечены как
backend-only.
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

#: Кому можно выдать доступ (зеркало ``AccessGrantTargetType``).
TARGET_TYPES = ("user", "company", "tag")

#: Уровни доступа (зеркало backend-валидации).
ROLES = ("viewer", "editor", "admin")


def _pick_target(
    skill: str | None, collection: str | None
) -> tuple[str, str]:
    """Выбрать объект гранта → ``("skill"|"collection", ref)``.

    Ровно один из двух: иначе непонятно, куда идёт запрос, а молчаливый
    приоритет одного над другим — источник тихих ошибок.
    """
    if bool(skill) == bool(collection):
        console.print("[red]Укажите ровно один из --skill / --collection[/]")
        raise typer.Exit(1)
    if skill:
        return "skill", skill
    return "collection", str(collection)


def _grants_table(grants: list[dict[str, Any]]) -> Table:
    table = Table(title=f"Гранты доступа (всего: {len(grants)})")
    table.add_column("grant_id")
    table.add_column("кому")
    table.add_column("id")
    table.add_column("имя", overflow="fold")
    table.add_column("роль")
    table.add_column("выдан")
    for grant in grants:
        table.add_row(
            str(grant.get("id") or "—"),
            str(grant.get("target_type") or "—"),
            str(grant.get("target_id") or "—"),
            str(grant.get("target_name") or "—"),
            str(grant.get("role") or "—"),
            str(grant.get("granted_at") or "—"),
        )
    return table


def cmd_access_list(
    skill: str | None = typer.Option(None, "--skill", help="Навык (id или slug)"),
    collection: str | None = typer.Option(
        None, "--collection", help="Коллекция (id)"
    ),
) -> None:
    """Кому выдан доступ к навыку/коллекции (GET …/access-grants).

    Право: ``skill.manage``/``hub.admin`` — эта ручка чтения у backend
    гейтится, в отличие от большинства GET'ов.
    """
    kind, ref = _pick_target(skill, collection)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            if kind == "skill":
                # У ручки в пути именно skill_id (числовой), slug она не
                # резолвит — приводим здесь, чтобы человек мог писать slug.
                skill_id = await _common.resolve_skill_id(client, ref)
                resp = await client.list_skill_access_grants(skill_id)
            else:
                resp = await client.list_collection_access_grants(ref)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            grants = payload.get("grants") or []
            if not grants:
                console.print("[yellow]Грантов нет[/]")
                return
            console.print(_grants_table(grants))

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_access_grant(
    skill: str | None = typer.Option(None, "--skill", help="Навык (id или slug)"),
    collection: str | None = typer.Option(
        None, "--collection", help="Коллекция (id)"
    ),
    target_type: str = typer.Option(
        ..., "--to", help=f"Кому: {' | '.join(TARGET_TYPES)}"
    ),
    target_id: str = typer.Option(..., "--target-id", help="ID субъекта"),
    role: str = typer.Option(
        "viewer", "--role", help=f"Уровень: {' | '.join(ROLES)}"
    ),
) -> None:
    """Выдать доступ (PUT …/access-grants). Идемпотентно.

    Повторный вызов на тот же таргет вернёт существующий грант, а не создаст
    второй.
    """
    kind, ref = _pick_target(skill, collection)
    if target_type not in TARGET_TYPES:
        console.print(f"[red]--to должен быть одним из: {', '.join(TARGET_TYPES)}[/]")
        raise typer.Exit(1)
    if role not in ROLES:
        console.print(f"[red]--role должен быть одним из: {', '.join(ROLES)}[/]")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            if kind == "skill":
                skill_id = await _common.resolve_skill_id(client, ref)
                resp = await client.grant_skill_access(
                    skill_id,
                    target_type=target_type,
                    target_id=target_id,
                    role=role,
                )
            else:
                resp = await client.grant_collection_access(
                    ref,
                    target_type=target_type,
                    target_id=target_id,
                    role=role,
                )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda g: console.print(
                f"[green]✓[/] Доступ выдан: {g.get('target_type')} "
                f"{g.get('target_id')} → {g.get('role')} "
                f"(grant_id={g.get('id')})"
            ),
        )

    _common.run(_do())


def cmd_access_revoke(
    grant_id: str = typer.Argument(..., help="ID гранта (см. `access list`)"),
    skill: str | None = typer.Option(None, "--skill", help="Навык (id или slug)"),
    collection: str | None = typer.Option(
        None, "--collection", help="Коллекция (id)"
    ),
) -> None:
    """Отозвать доступ (DELETE …/access-grants/{grant_id})."""
    kind, ref = _pick_target(skill, collection)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            if kind == "skill":
                skill_id = await _common.resolve_skill_id(client, ref)
                await client.revoke_skill_access(skill_id, grant_id)
            else:
                await client.revoke_collection_access(ref, grant_id)
        finally:
            await client.close()

        emit_data(
            {"revoked": True, "grant_id": grant_id, "target_kind": kind},
            text_renderer=lambda _p: console.print(
                f"[green]✓[/] Грант {grant_id} отозван"
            ),
        )

    _common.run(_do())


def register(app: typer.Typer, *, can_manage: bool = False) -> None:
    """Регистрирует группу ``access`` под гейтом ``skill.manage``/``hub.admin``.

    Гейт закрывает и чтение: у backend ``GET …/access-grants`` тоже требует
    прав, поэтому показывать команду тем, кто гарантированно получит 403,
    смысла нет.
    """
    if not can_manage:
        return
    access_app = typer.Typer(
        no_args_is_help=True,
        help="Доступ к навыкам и коллекциям: кому выдан, выдать, отозвать.",
    )
    access_app.command("list")(cmd_access_list)
    access_app.command("grant")(cmd_access_grant)
    access_app.command("revoke")(cmd_access_revoke)
    app.add_typer(access_app, name="access")
