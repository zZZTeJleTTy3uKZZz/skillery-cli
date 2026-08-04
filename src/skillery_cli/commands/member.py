"""``skillery members`` / ``skillery member`` / ``skillery roles`` — P1 C3.

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
  (``POST /users/bulk/change-role`` с одним user_id).
- ``member lock <user_id> [--reason]`` / ``member unlock <user_id>`` —
  блокировка входа (``PUT /users/{id}/lock|unlock``).
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
from clikit.command_kit import gated
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data, emit_error

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
        ..., "--role-id", help="ID роли (см. `skillery roles`)"
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
        ..., help="ID новой роли (см. `skillery roles`)"
    ),
    company: str | None = typer.Option(
        None, "--company", help="ID компании (default: из вашего токена)"
    ),
) -> None:
    """Сменить роль участника (POST /users/bulk/change-role одним user_id).

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
    """Заблокировать вход пользователю (PUT /users/{id}/lock).

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
    """Снять блокировку входа (PUT /users/{id}/unlock). Право ``user.lock``."""
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
                    "пароль (skillery passwd / профиль Web UI).[/]"
                )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def _bulk_status_outcome(p: dict[str, Any], user_id: str) -> str:
    """Outcome для одиночной bulk-операции (suspend/activate) над user_id."""
    results = p.get("results") or []
    if results:
        return results[0].get("outcome", "—")
    return "updated" if p.get("updated_count") else "skipped"


def cmd_member_suspend(
    user_id: str = typer.Argument(..., help="ID пользователя"),
) -> None:
    """Приостановить пользователя (POST /users/bulk/suspend одним user_id).

    Право ``user.lock``/``company.manage`` (зеркало backend ``_can_admin_users``;
    bulk-эндпоинт допускает company-admin над своими). Активные сессии
    отзываются (status=suspended).
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            r = await client.bulk_suspend(user_ids=[user_id])
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            outcome = _bulk_status_outcome(p, user_id)
            if outcome == "updated":
                console.print(
                    f"[green]✓[/] Пользователь {user_id} приостановлен "
                    "[dim](сессии отозваны)[/]"
                )
            else:
                console.print(
                    f"[yellow]Не изменён[/]: {user_id} → {outcome}"
                )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_member_activate(
    user_id: str = typer.Argument(..., help="ID пользователя"),
) -> None:
    """Активировать пользователя (POST /users/bulk/activate одним user_id).

    Право как у suspend (``_can_admin_users``). Ставит status=active.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            r = await client.bulk_activate(user_ids=[user_id])
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            outcome = _bulk_status_outcome(p, user_id)
            if outcome == "updated":
                console.print(
                    f"[green]✓[/] Пользователь {user_id} активирован"
                )
            else:
                console.print(
                    f"[yellow]Не изменён[/]: {user_id} → {outcome}"
                )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_member_revoke_sessions(
    user_id: str = typer.Argument(..., help="ID пользователя"),
) -> None:
    """Отозвать все сессии пользователя (POST /users/{id}/revoke-sessions).

    «Выйти со всех устройств»: живые access-токены → 401, refresh-токены
    отозваны, статус НЕ меняется. Право ``user.lock``/``company.manage`` (или
    self). В отличие от lock — вход остаётся разрешён.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.revoke_user_sessions(user_id)
        finally:
            await client.close()
        # REST-10 (#1452): DELETE отдаёт 204 без тела — печатаем сам факт.
        emit_data(
            {"user_id": user_id, "revoked": True},
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Сессии отозваны: {p['user_id']} "
                "[dim](пользователь вышел со всех устройств)[/]"
            ),
        )

    _common.run(_do())


def cmd_member_create(
    email: str = typer.Option(..., "--email", help="Email пользователя"),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Display-name (если опущен — часть email до @)",
    ),
    role_id: str | None = typer.Option(
        None, "--role-id", help="ID роли (опц.; без неё — роль «Участник»)"
    ),
    first_name: str | None = typer.Option(None, "--first-name"),
    last_name: str | None = typer.Option(None, "--last-name"),
    set_password: str | None = typer.Option(
        None,
        "--set-password",
        help="Задать пароль сразу (≥8 символов; иначе — без пароля)",
    ),
    company: str | None = typer.Option(
        None, "--company", help="ID компании (default: из вашего токена)"
    ),
) -> None:
    """Создать пользователя (POST /users).

    Право ``user.create``/``company.manage`` (hub.admin — любую компанию;
    company-admin — только свою). Если email уже есть — добавляется membership
    (is_new_user=False). В отличие от ``member invite`` — без invite-токена.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    company_id = _resolve_company_id(cfg, company, required=True)
    display_name = name or _derive_display_name(email)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            r = await client.create_user(
                email=email,
                display_name=display_name,
                company_id=company_id,
                role_id=role_id,
                first_name=first_name,
                last_name=last_name,
                set_password=set_password,
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            is_new = p.get("is_new_user")
            console.print(
                f"[green]✓[/] Пользователь {email} "
                f"(id={p.get('user_id')}, company={company_id})"
            )
            if is_new:
                console.print("  [dim](создан новый user)[/]")
            else:
                console.print(
                    "  [dim](существующий user — добавлен membership)[/]"
                )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_member_edit(
    user_id: str = typer.Argument(..., help="ID пользователя"),
    name: str | None = typer.Option(None, "--name", help="Новый display-name"),
    first_name: str | None = typer.Option(None, "--first-name"),
    last_name: str | None = typer.Option(None, "--last-name"),
    status: str | None = typer.Option(
        None, "--status", help="active | invited | suspended"
    ),
) -> None:
    """Изменить пользователя (PATCH /users/{id}, merge-patch).

    Право ``user.update``/``company.manage`` (или self). Шлём только заданные
    поля. ``--status suspended`` отзывает refresh-токены.
    """
    cfg = ClientConfig.load()
    payload: dict[str, Any] = {}
    if name is not None:
        payload["display_name"] = name
    if first_name is not None:
        payload["first_name"] = first_name
    if last_name is not None:
        payload["last_name"] = last_name
    if status is not None:
        if status not in ("active", "invited", "suspended"):
            emit_error(
                "VALIDATION",
                "--status должен быть active|invited|suspended, "
                f"получено: {status}",
            )
            raise typer.Exit(1)
        payload["status"] = status
    if not payload:
        emit_error(
            "VALIDATION",
            "Нечего менять: укажите --name/--first-name/--last-name/--status",
        )
        raise typer.Exit(1)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            u = await client.update_user(user_id, payload)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Пользователь обновлён: "
                f"{p.get('email') or user_id} (id={p.get('id') or user_id})"
            )
            console.print(f"  Поля: {', '.join(sorted(payload))}")

        emit_data(u, text_renderer=_render)

    _common.run(_do())


def cmd_member_delete(
    user_id: str = typer.Argument(..., help="ID пользователя"),
) -> None:
    """Удалить пользователя (DELETE /users/{id}, soft-delete).

    Право ``user.delete``/``company.manage``. Self-delete запрещён (409).
    Refresh-токены отзываются.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.delete_user(user_id)
        finally:
            await client.close()
        emit_data(
            {"event": "user_deleted", "user_id": user_id},
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Пользователь {p['user_id']} удалён "
                "[dim](soft-delete, сессии отозваны)[/]"
            ),
        )

    _common.run(_do())


def cmd_member_transfer(
    user_id: str = typer.Argument(..., help="ID пользователя"),
    new_company: str = typer.Option(
        ..., "--to-company", help="ID компании назначения"
    ),
    new_role_id: str = typer.Option(
        ..., "--role-id", help="ID роли в новой компании"
    ),
    keep_old: bool = typer.Option(
        False,
        "--keep-old",
        help="Оставить членство в прежней компании (default: убрать)",
    ),
) -> None:
    """Перенести пользователя в другую компанию (POST /users/{id}/transfer).

    Только hub.admin. По умолчанию старые memberships удаляются; ``--keep-old``
    оставляет пользователя в обеих компаниях.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            u = await client.transfer_user(
                user_id,
                new_company_id=new_company,
                new_role_id=new_role_id,
                keep_old_membership=keep_old,
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Пользователь {p.get('email') or user_id} "
                f"перенесён в компанию {new_company} (role={new_role_id})"
            )
            if keep_old:
                console.print(
                    "  [dim](членство в прежней компании сохранено)[/]"
                )

        emit_data(u, text_renderer=_render)

    _common.run(_do())


def cmd_member_export(
    company: str | None = typer.Option(
        None, "--company", help="Фильтр по компании"
    ),
    q: str | None = typer.Option(
        None, "--q", help="Фильтр по email (подстрока)"
    ),
    status: str | None = typer.Option(
        None, "--status", help="Фильтр по статусу"
    ),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Файл для CSV (default: stdout)"
    ),
) -> None:
    """Экспорт пользователей в CSV (GET /users/export.csv). Только hub.admin.

    Без ``--output`` CSV печатается в stdout; с ним — пишется в файл.
    Columns: id, email, first_name, last_name, status, last_login_at,
    created_at.
    """
    from pathlib import Path

    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            csv_text = await client.export_users(
                company_id=company, q=q, status=status
            )
        finally:
            await client.close()
        if output:
            Path(output).write_text(csv_text, encoding="utf-8")
            emit_data(
                {
                    "event": "users_exported",
                    "output": output,
                    "rows": max(csv_text.count("\n") - 1, 0),
                },
                text_renderer=lambda p: console.print(
                    f"[green]✓[/] Экспортировано в {p['output']} "
                    f"({p['rows']} строк)"
                ),
            )
        else:
            # CSV — машинный текст: печатаем как есть (raw), без rich-разметки.
            emit_data(
                {"event": "users_exported", "csv": csv_text},
                text_renderer=lambda p: print(p["csv"], end=""),
            )

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
    can_create: bool = False,
    can_update: bool = False,
    can_delete: bool = False,
    can_transfer: bool = False,
    can_export: bool = False,
) -> None:
    """Регистрирует ``members``/``roles`` (любой залогиненный) + sub-app
    ``member`` (подкоманды по правам; без единого права sub-app не
    создаётся вовсе — как admin sub-app в ``build_app``).

    M-3: ``suspend``/``activate``/``revoke-sessions`` гейтятся ``can_lock``
    (предикат ``_can_admin_users`` — зеркало backend bulk-эндпоинтов).
    M-5: ``create``/``edit``/``delete``/``transfer``/``export`` —
    отдельными правами (``user.create``/``user.update``/``user.delete``/
    hub.admin для transfer+export).
    """
    app.command(name="members")(cmd_members_list)
    app.command(name="roles")(cmd_roles_list)
    if not any(
        (
            can_invite,
            can_remove,
            can_change_role,
            can_lock,
            can_reset_password,
            can_create,
            can_update,
            can_delete,
            can_transfer,
            can_export,
        )
    ):
        return
    member_app = typer.Typer(
        no_args_is_help=True,
        help="Управление участниками компании (по правам)",
    )
    # cli-kits W6: одиночные permission-гейты → command_kit.gated. Предикаты —
    # предвычисленные булевы (зеркало backend per-action), has_permission —
    # lambda над готовым флагом.
    gated(member_app, permission="invite", has_permission=lambda _p: can_invite,
          name="invite")(cmd_member_invite)
    gated(member_app, permission="create", has_permission=lambda _p: can_create,
          name="create")(cmd_member_create)
    gated(member_app, permission="edit", has_permission=lambda _p: can_update,
          name="edit")(cmd_member_edit)
    gated(member_app, permission="delete", has_permission=lambda _p: can_delete,
          name="delete")(cmd_member_delete)
    gated(member_app, permission="remove", has_permission=lambda _p: can_remove,
          name="remove")(cmd_member_remove)
    gated(member_app, permission="change-role",
          has_permission=lambda _p: can_change_role,
          name="change-role")(cmd_member_change_role)
    if can_lock:
        member_app.command("lock")(cmd_member_lock)
        member_app.command("unlock")(cmd_member_unlock)
        # M-3: suspend/activate/revoke-sessions — тот же предикат
        # (_can_admin_users), что lock/unlock, зеркало bulk-эндпоинтов бэка.
        member_app.command("suspend")(cmd_member_suspend)
        member_app.command("activate")(cmd_member_activate)
        member_app.command("revoke-sessions")(cmd_member_revoke_sessions)
    gated(member_app, permission="reset-password",
          has_permission=lambda _p: can_reset_password,
          name="reset-password")(cmd_member_reset_password)
    gated(member_app, permission="transfer",
          has_permission=lambda _p: can_transfer,
          name="transfer")(cmd_member_transfer)
    gated(member_app, permission="export", has_permission=lambda _p: can_export,
          name="export")(cmd_member_export)
    app.add_typer(member_app, name="member")
