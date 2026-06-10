"""``skills-hub members`` / ``skills-hub member`` / ``skills-hub roles`` — P1 C3.

Участники компании + глобальный каталог ролей.

Команды:
- ``members [--company --q --page --size]`` — список участников
  (``GET /users?company_id=``; member без admin-прав видит только себя —
  бэк сужает выдачу сам).
- ``member invite --email ... --role-id ... [--name] [--company]`` —
  пригласить (flat ``POST /invites``, переиспользует
  ``transport.issue_invite``).
- ``member remove <user_id> [--company]`` — убрать из компании
  (``DELETE /memberships``).
- ``member change-role <user_id> <role_id> [--company]`` — смена роли
  (``POST /users/bulk/change_role`` с одним user_id).
- ``member lock <user_id> [--reason]`` / ``member unlock <user_id>`` —
  блокировка входа (``POST /users/{id}/lock|unlock``).
- ``member reset-password <user_id>`` — одноразовый пароль
  (``POST /users/{id}/reset-password``; показывается ОДИН раз).
- ``roles`` — глобальный каталог ролей (``GET /roles``, paged) для
  выбора role_id.

Permissions (CLI-гейты в ``register``; hub.admin — bypass внутри
``ClientConfig.has_permission``): members/roles — любой залогиненный;
invite → user.invite; remove → user.remove; change-role → role.manage;
lock/unlock → user.lock; reset-password → company.manage.

Дефолт ``--company`` везде — company_id из cfg (JWT текущей сессии).
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.output import emit_data, emit_error

console = Console()


def _resolve_company_id(
    cfg: ClientConfig, company: str | None, *, required: bool
) -> str | None:
    """``--company`` либо company_id из cfg (JWT). ``required`` → exit(1)."""
    cid = company or cfg.company_id
    if required and not cid:
        emit_error(
            "VALIDATION",
            "company_id не определён: укажите --company "
            "(в вашем токене нет company_id)",
        )
        raise typer.Exit(1)
    return cid


def _derive_display_name(email: str) -> str:
    """Local-part email'а как display_name (бэк требует email+name вместе)."""
    return email.split("@", 1)[0]


def _role_label_for(item: dict[str, Any], company_id: str | None) -> str:
    """Роль участника в выбранной компании (или первая из memberships)."""
    memberships = item.get("memberships") or []
    if company_id:
        for ms in memberships:
            comp = ms.get("company") or {}
            if str(comp.get("id")) == str(company_id):
                role = ms.get("role") or {}
                return role.get("label") or role.get("slug") or "—"
    if memberships:
        role = memberships[0].get("role") or {}
        return role.get("label") or role.get("slug") or "—"
    return "—"


def cmd_members_list(
    company: str | None = typer.Option(
        None,
        "--company",
        help="ID компании (default: company_id из вашего токена)",
    ),
    q: str | None = typer.Option(
        None, "--q", help="Поиск по email или имени (подстрока)"
    ),
    page: int = typer.Option(1, "--page", min=1),
    size: int = typer.Option(25, "--size", min=1, max=200),
) -> None:
    """Участники компании (GET /users, серверная пагинация).

    Без admin-прав backend возвращает только вас самих. hub-admin без
    ``--company`` видит всех пользователей хаба.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    # members — company_id НЕ обязателен: hub-admin без него видит всех.
    company_id = _resolve_company_id(cfg, company, required=False)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_users(
                company_id=company_id, q=q, page=page, size=size
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            items = p.get("items") or []
            total = p.get("total") or 0
            eff_size = p.get("size") or size
            pages = (total + eff_size - 1) // max(eff_size, 1)
            title = f"Участники (total={total} стр. {p.get('page') or page}/{max(pages, 1)})"
            if company_id:
                title += f" company={company_id}"
            if not items:
                console.print("[yellow]Участников не найдено[/]")
                return
            table = Table(title=title)
            table.add_column("id")
            table.add_column("email")
            table.add_column("имя")
            table.add_column("роль")
            table.add_column("статус")
            for u in items:
                status = u.get("status") or "—"
                if u.get("is_locked"):
                    status += " 🔒"
                table.add_row(
                    str(u.get("id")),
                    u.get("email") or "—",
                    u.get("display_name") or "—",
                    _role_label_for(u, company_id),
                    status,
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_member_invite(
    email: str = typer.Option(..., "--email", help="Email приглашаемого"),
    role_id: str = typer.Option(
        ..., "--role-id", help="ID роли (см. `skills-hub roles`)"
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help=(
            "Display-name приглашаемого. Если опущен — берётся часть email "
            "до @ (backend требует email и имя строго вместе)."
        ),
    ),
    company: str | None = typer.Option(
        None, "--company", help="ID компании (default: из вашего токена)"
    ),
) -> None:
    """Пригласить участника в компанию (flat POST /invites).

    Право ``user.invite``. Создаёт pre-emptive User(invited)+Membership —
    приглашённый сразу виден в ``members``. Печатает invite-token и URL.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    company_id = _resolve_company_id(cfg, company, required=True)
    display_name = name or _derive_display_name(email)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            r = await client.issue_invite(
                company_id, role_id, email=email, display_name=display_name
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Invite для {email} (role={role_id}, "
                f"company={company_id})"
            )
            console.print(f"  Token: {p['invite_token']}")
            console.print(f"  URL:   {p['invite_url']}")
            if p.get("is_new_user"):
                console.print("  [dim](создан новый user со статусом invited)[/]")
            if p.get("expires_at"):
                console.print(f"  [dim]Истекает: {p['expires_at']}[/]")

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_member_remove(
    user_id: str = typer.Argument(..., help="ID пользователя"),
    company: str | None = typer.Option(
        None, "--company", help="ID компании (default: из вашего токена)"
    ),
) -> None:
    """Убрать участника из компании (DELETE /memberships).

    Право ``user.remove``. Backend дополнительно отзывает refresh-токены
    удалённого участника.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    company_id = _resolve_company_id(cfg, company, required=True)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.remove_membership(
                user_id=user_id, company_id=company_id
            )
        finally:
            await client.close()
        emit_data(
            {
                "event": "member_removed",
                "user_id": user_id,
                "company_id": company_id,
            },
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Участник {p['user_id']} удалён из компании "
                f"{p['company_id']} [dim](его сессии отозваны)[/]"
            ),
        )

    _common.run(_do())


def cmd_member_change_role(
    user_id: str = typer.Argument(..., help="ID пользователя"),
    role_id: str = typer.Argument(
        ..., help="ID новой роли (см. `skills-hub roles`)"
    ),
    company: str | None = typer.Option(
        None, "--company", help="ID компании (default: из вашего токена)"
    ),
) -> None:
    """Сменить роль участника (POST /users/bulk/change_role одним user_id).

    Право ``role.manage`` в компании (или hub.admin). Не-assignable роль
    для company-admin отклоняется backend'ом (422).
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    company_id = _resolve_company_id(cfg, company, required=True)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            r = await client.bulk_change_role(
                user_ids=[user_id], role_id=role_id, company_id=company_id
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            results = p.get("results") or []
            outcome = results[0]["outcome"] if results else (
                "updated" if p.get("updated_count") else "skipped"
            )
            if outcome == "updated":
                console.print(
                    f"[green]✓[/] Роль участника {user_id} → {role_id} "
                    f"(company={company_id})"
                )
            else:
                console.print(
                    f"[yellow]Роль не изменена[/]: {user_id} → {outcome}"
                )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_member_lock(
    user_id: str = typer.Argument(..., help="ID пользователя"),
    reason: str | None = typer.Option(
        None, "--reason", help="Причина блокировки (попадает в audit)"
    ),
) -> None:
    """Заблокировать вход пользователю (POST /users/{id}/lock).

    Право ``user.lock`` (или hub.admin). Активные сессии отзываются,
    self-lock запрещён backend'ом.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            u = await client.lock_user(user_id, reason=reason)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Заблокирован: {p.get('email') or user_id} "
                f"(is_locked={p.get('is_locked')})"
            )
            if reason:
                console.print(f"  [dim]Причина: {reason}[/]")

        emit_data(u, text_renderer=_render)

    _common.run(_do())


def cmd_member_unlock(
    user_id: str = typer.Argument(..., help="ID пользователя"),
) -> None:
    """Снять блокировку входа (POST /users/{id}/unlock). Право ``user.lock``."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            u = await client.unlock_user(user_id)
        finally:
            await client.close()
        emit_data(
            u,
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Разблокирован: {p.get('email') or user_id} "
                f"(is_locked={p.get('is_locked')})"
            ),
        )

    _common.run(_do())


def cmd_member_reset_password(
    user_id: str = typer.Argument(..., help="ID пользователя"),
) -> None:
    """Сгенерировать одноразовый пароль (POST /users/{id}/reset-password).

    Право ``company.manage`` (или hub.admin). Пароль отображается ТОЛЬКО
    ОДИН раз — backend хранит лишь хэш; активные сессии пользователя
    отзываются.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            r = await client.reset_user_password(user_id)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(f"[green]✓[/] Пароль сброшен для user {user_id}")
            console.print(
                Panel(
                    f"[bold yellow]{p['temp_password']}[/]",
                    title="ВРЕМЕННЫЙ ПАРОЛЬ",
                    expand=False,
                )
            )
            console.print(
                "[red]⚠ Показывается ОДИН раз — передайте пользователю "
                "сейчас (повторно получить нельзя).[/]"
            )
            if p.get("expires_hint"):
                console.print(f"[dim]{p['expires_hint']}[/]")
            if p.get("requires_password_change"):
                console.print(
                    "[dim]При первом входе пользователь должен сменить "
                    "пароль (skills-hub passwd / профиль Web UI).[/]"
                )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_roles_list() -> None:
    """Глобальный каталог ролей (GET /roles) — для выбора role_id.

    Любой залогиненный. Ответ backend'а paged; CLI показывает первые 100
    (после W3 каталог глобальный и маленький).
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_roles(page=1, size=100)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            items = p.get("items") or []
            total = p.get("total") or len(items)
            title = f"Роли (total={total})"
            if total > len(items):
                title += f" — показаны первые {len(items)}"
            if not items:
                console.print("[yellow]Ролей не найдено[/]")
                return
            table = Table(title=title)
            table.add_column("id")
            table.add_column("slug")
            table.add_column("name")
            table.add_column("assignable by company")
            for r in items:
                table.add_row(
                    str(r.get("id")),
                    r.get("slug") or "—",
                    r.get("name") or "—",
                    "✓" if r.get("is_assignable_by_company") else "— (hub.admin)",
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def register(
    app: typer.Typer,
    *,
    can_invite: bool = False,
    can_remove: bool = False,
    can_change_role: bool = False,
    can_lock: bool = False,
    can_reset_password: bool = False,
) -> None:
    """Регистрирует ``members``/``roles`` (любой залогиненный) + sub-app
    ``member`` (подкоманды по правам; без единого права sub-app не
    создаётся вовсе — как admin sub-app в ``build_app``)."""
    app.command(name="members")(cmd_members_list)
    app.command(name="roles")(cmd_roles_list)
    if not any(
        (can_invite, can_remove, can_change_role, can_lock, can_reset_password)
    ):
        return
    member_app = typer.Typer(
        no_args_is_help=True,
        help="Управление участниками компании (по правам)",
    )
    if can_invite:
        member_app.command("invite")(cmd_member_invite)
    if can_remove:
        member_app.command("remove")(cmd_member_remove)
    if can_change_role:
        member_app.command("change-role")(cmd_member_change_role)
    if can_lock:
        member_app.command("lock")(cmd_member_lock)
        member_app.command("unlock")(cmd_member_unlock)
    if can_reset_password:
        member_app.command("reset-password")(cmd_member_reset_password)
    app.add_typer(member_app, name="member")
