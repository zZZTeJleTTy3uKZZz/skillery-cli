"""``skills-hub company`` — компании + invite-links + каталог (P1, эпик C2).

Подкоманды:

- ``company list|show|create|edit|switch`` — CRUD + переключение активной
  компании (re-issue токенов под целевую компанию).
- ``company invite-links list|create|revoke`` — переиспользуемые
  пригласительные ссылки; create печатает ГОТОВЫЙ join-URL
  (``<web-ui>/join/<token>`` — тот же формат, что строит Web).
- ``company catalog list|grant|revoke`` — granted-каталог компании
  (``--collection`` переключает на collections-эндпоинты).

``--company`` опционален: по умолчанию берётся активная компания из JWT/cfg.
Гейты видимости (см. ``register``) зеркалят backend-права; backend всё равно
финально режет 403.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig, populate_from_jwt, save_tokens
from skills_hub_cli.output import emit_data, emit_error

console = Console()


def _resolve_company_id(cfg: ClientConfig, company: str | None) -> str:
    """``--company`` либо активная компания из cfg (JWT).

    Нет ни того ни другого → внятная ошибка (у пользователя нет активной
    компании — например, hub-admin без membership) + exit 1.
    """
    if company:
        return company
    if cfg.company_id:
        return str(cfg.company_id)
    emit_error(
        "NO_COMPANY",
        "Активная компания не определена (в JWT нет company_id). "
        "Укажите её явно: --company <id>.",
    )
    raise typer.Exit(1)


async def _resolve_collection_id(client: Any, id_or_slug: str) -> str:
    """Привести id-или-slug коллекции к числовому id строкой.

    Catalog-эндпоинты коллекций принимают СТРОГО числовой id
    (``routes/catalog.py::_int_id``) — slug резолвим через
    ``GET /collections/{slug}`` (``CollectionDetailResponse.collection.id``).
    """
    if id_or_slug.isdigit():
        return id_or_slug
    data: dict[str, Any] = await client.get_collection(id_or_slug)
    return str(data["collection"]["id"])


# ======================================================
#                    company CRUD
# ======================================================
def cmd_company_list(
    q: str | None = typer.Option(None, "--q", help="Поиск по названию/slug"),
    page: int = typer.Option(1, "--page", help="Номер страницы (1-based)"),
    size: int | None = typer.Option(
        None, "--size", help="Размер страницы (default backend, max 200)"
    ),
) -> None:
    """[hub.admin] Список компаний (server-side пагинация)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_companies(q=q, page=page, size=size)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            items = p.get("items") or []
            total = p.get("total")
            table = Table(title=f"Компании (всего: {total})")
            table.add_column("id")
            table.add_column("slug")
            table.add_column("name", overflow="fold")
            table.add_column("plan")
            table.add_column("status")
            for c in items:
                table.add_row(
                    str(c.get("id", "")),
                    c.get("slug") or "—",
                    c.get("name", ""),
                    c.get("plan", ""),
                    c.get("status", ""),
                )
            if not items:
                console.print("[yellow]Компаний нет[/]")
            else:
                console.print(table)
                cur_size = p.get("size") or len(items) or 1
                pages = ((total or 0) + cur_size - 1) // cur_size
                console.print(
                    f"[dim]стр. {p.get('page', 1)} из {max(pages, 1)}[/]"
                )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_company_show(
    company_id: str = typer.Argument(..., metavar="ID", help="id компании"),
) -> None:
    """Детали компании (член компании или hub.admin)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.get_company(company_id)
        finally:
            await client.close()

        def _render(c: dict[str, Any]) -> None:
            console.print(f"[bold]{c['name']}[/] (id={c['id']})")
            console.print(f"  slug:       {c.get('slug') or '—'}")
            console.print(f"  plan:       {c.get('plan')}   status: {c.get('status')}")
            console.print(f"  участники:  {c.get('members_count', 0)}")
            owner = c.get("owner") or {}
            console.print(
                f"  владелец:   "
                f"{owner.get('display_name') or owner.get('email') or '—'}"
            )
            domains = c.get("allowed_domains") or []
            if domains:
                console.print(f"  домены:     {', '.join(domains)}")
            console.print(f"  создана:    {c.get('created_at')}")

        emit_data(data, text_renderer=_render)

    _common.run(_do())


def cmd_company_create(
    name: str = typer.Option(..., "--name", help="Название компании"),
    owner_email: str = typer.Option(..., "--owner-email"),
    owner_name: str = typer.Option(..., "--owner-name"),
    slug: str | None = typer.Option(
        None,
        "--slug",
        help="Slug компании (требует hub.slug_manage; опусти → slug-less)",
    ),
) -> None:
    """[hub.company_create] Создать компанию + invite owner'у.

    Каноничная P1-локация ``admin company-create`` (старая команда сохранена).
    Пустой slug не шлём — backend создаст slug-less компанию.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        payload: dict[str, Any] = {
            "name": name,
            "owner_email": owner_email,
            "owner_display_name": owner_name,
        }
        if slug:
            payload["slug"] = slug
        try:
            r = await client.create_company(payload)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            label = p.get("slug") or p.get("company_id")
            console.print(f"[green]✓[/] Компания {label} (id={p['company_id']})")
            console.print(f"  Owner invite: {p['owner_invite_token']}")
            console.print(f"  URL:          {p['owner_invite_url']}")

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_company_edit(
    company_id: str = typer.Argument(..., metavar="ID", help="id компании"),
    name: str | None = typer.Option(None, "--name", help="Новое название"),
    owner_id: str | None = typer.Option(
        None, "--owner-id", help="Новый владелец (user id)"
    ),
) -> None:
    """[company.manage|hub.admin] Изменить компанию (merge-patch)."""
    cfg = ClientConfig.load()
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if owner_id is not None:
        payload["owner_id"] = owner_id
    if not payload:
        emit_error(
            "VALIDATION", "Нечего менять: укажите --name и/или --owner-id"
        )
        raise typer.Exit(1)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.update_company(company_id, payload)
        finally:
            await client.close()

        def _render(c: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Компания обновлена: {c['name']} (id={c['id']})"
            )
            console.print(f"  Поля: {', '.join(sorted(payload))}")

        emit_data(data, text_renderer=_render)

    _common.run(_do())


def cmd_company_switch(
    company_id: str = typer.Argument(
        ..., metavar="ID", help="id компании (нужен membership в ней)"
    ),
) -> None:
    """Переключить активную компанию (re-issue токенов).

    Backend (``POST /me/active-company``) отвечает НОВОЙ парой токенов с
    permissions роли в целевой компании — CLI сохраняет пару в keyring и
    обновляет cfg из нового JWT (как при login).
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.switch_active_company(company_id)
        finally:
            await client.close()
        # Ответ — LoginPasswordResponse: новая пара под целевую компанию.
        save_tokens(
            cfg.user_email or "", data["access_token"], data["refresh_token"]
        )
        populate_from_jwt(cfg, data["access_token"])
        cfg.save()
        result = {
            "event": "company_switched",
            "company_id": cfg.company_id,
            "role_id": cfg.role_id,
            "permissions": cfg.permissions,
            "access_expires_at": cfg.access_expires_at,
        }

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Активная компания: {p['company_id']} "
                f"(role_id={p['role_id'] or '—'})"
            )
            console.print(f"  Permissions: {len(p['permissions'])} прав")
            console.print(
                "[dim]Токены перевыпущены — набор команд мог измениться "
                "(`skills-hub --help`)[/]"
            )

        emit_data(result, text_renderer=_render)

    _common.run(_do())


# ======================================================
#                    invite-links
# ======================================================
def cmd_invite_links_list(
    company: str | None = typer.Option(
        None, "--company", help="id компании (default: активная из JWT)"
    ),
) -> None:
    """Список переиспользуемых пригласительных ссылок компании."""
    cfg = ClientConfig.load()
    cid = _resolve_company_id(cfg, company)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.list_invite_links(cid)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            # Канон (волна 3): список = ``items``; ``links`` сохранён
            # бэкендом как deprecated-дубль (fallback).
            links = p.get("items") or p.get("links") or []
            if not links:
                console.print(f"[yellow]Ссылок нет[/] (company={cid})")
                return
            table = Table(title=f"Invite-links (company={cid})")
            table.add_column("id")
            table.add_column("kind")
            table.add_column("active")
            table.add_column("uses")
            table.add_column("expires_at")
            for lk in links:
                uses = str(lk.get("used_count", 0))
                if lk.get("max_uses"):
                    uses += f"/{lk['max_uses']}"
                table.add_row(
                    str(lk.get("id", "")),
                    lk.get("kind", ""),
                    "✓" if lk.get("is_active") else "✗",
                    uses,
                    str(lk.get("expires_at") or "—"),
                )
            console.print(table)

        emit_data(data, text_renderer=_render)

    _common.run(_do())


def cmd_invite_links_create(
    kind: str = typer.Option(
        "member", "--kind", help="member | manager (manager — только владелец)"
    ),
    max_uses: int | None = typer.Option(
        None, "--max-uses", help="Лимит использований (default: без лимита)"
    ),
    expires_in_days: int | None = typer.Option(
        None, "--expires-in-days", help="Срок жизни в днях, 1..365"
    ),
    company: str | None = typer.Option(
        None, "--company", help="id компании (default: активная из JWT)"
    ),
) -> None:
    """Создать пригласительную ссылку — печатает ГОТОВЫЙ join-URL.

    join-URL = ``<web-ui>/join/<token>`` — тот же формат, что строит Web UI
    (``CompanyInviteLinks``); токен показывается ОДИН раз.
    """
    if kind not in ("member", "manager"):
        emit_error("VALIDATION", f"kind должен быть member|manager, получено: {kind}")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    cid = _resolve_company_id(cfg, company)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.create_invite_link(
                cid,
                kind=kind,
                max_uses=max_uses,
                expires_in_days=expires_in_days,
            )
        finally:
            await client.close()
        web_base = cfg.effective_web_ui_url().rstrip("/")
        join_url = f"{web_base}/join/{data['token']}"
        payload = {**data, "join_url": join_url, "company_id": cid}

        def _render(p: dict[str, Any]) -> None:
            console.print(f"[green]✓[/] Ссылка создана (kind={p['kind']})")
            console.print(f"  Join URL: {p['join_url']}")
            if p.get("max_uses"):
                console.print(f"  Лимит:    {p['max_uses']} использований")
            if p.get("expires_at"):
                console.print(f"  Истекает: {p['expires_at']}")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_invite_links_revoke(
    link_id: str = typer.Argument(..., metavar="LINK_ID", help="id ссылки"),
    company: str | None = typer.Option(
        None, "--company", help="id компании (default: активная из JWT)"
    ),
) -> None:
    """Отозвать пригласительную ссылку."""
    cfg = ClientConfig.load()
    cid = _resolve_company_id(cfg, company)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.revoke_invite_link(cid, link_id)
        finally:
            await client.close()
        emit_data(
            {"event": "invite_link_revoked", "company_id": cid, "link_id": link_id},
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Ссылка {p['link_id']} отозвана"
            ),
        )

    _common.run(_do())


# ======================================================
#                    catalog
# ======================================================
def cmd_catalog_list(
    company: str | None = typer.Option(
        None, "--company", help="id компании (default: активная из JWT)"
    ),
) -> None:
    """Granted-каталог компании: навыки + коллекции + effective skills."""
    cfg = ClientConfig.load()
    cid = _resolve_company_id(cfg, company)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.get_company_catalog(cid)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            def _refs(items: list) -> str:
                return (
                    ", ".join(
                        str(i.get("slug") or i.get("id")) for i in items
                    )
                    or "—"
                )

            console.print(f"[bold]Каталог компании {p.get('company_id')}[/]")
            console.print(f"  Навыки:     {_refs(p.get('skills') or [])}")
            console.print(f"  Коллекции:  {_refs(p.get('collections') or [])}")
            console.print(
                f"  Effective:  {len(p.get('effective_skills') or [])} навыков"
            )

        emit_data(data, text_renderer=_render)

    _common.run(_do())


def cmd_catalog_grant(
    ref: str = typer.Argument(
        ...,
        metavar="ID_ИЛИ_SLUG",
        help="Навык (или коллекция при --collection): id-или-slug",
    ),
    collection: bool = typer.Option(
        False,
        "--collection",
        help="Выдать КОЛЛЕКЦИЮ вместо навыка (collections-эндпоинт)",
    ),
    company: str | None = typer.Option(
        None, "--company", help="id компании (default: активная из JWT)"
    ),
) -> None:
    """[catalog.manage] Выдать компании навык (или коллекцию: --collection).

    POST-эндпоинты каталога принимают строго числовой id — slug резолвится
    автоматически (GET /skills/{slug} | GET /collections/{slug}).
    """
    cfg = ClientConfig.load()
    cid = _resolve_company_id(cfg, company)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            if collection:
                coll_id = await _resolve_collection_id(client, ref)
                await client.grant_catalog_collection(cid, coll_id)
                granted: dict[str, Any] = {
                    "kind": "collection",
                    "id": coll_id,
                }
            else:
                skill_id = await _common.resolve_skill_id(client, ref)
                await client.grant_catalog_skill(cid, skill_id)
                granted = {"kind": "skill", "id": skill_id}
        finally:
            await client.close()
        emit_data(
            {
                "event": "catalog_granted",
                "company_id": cid,
                "ref": ref,
                **granted,
            },
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Выдано компании {p['company_id']}: "
                f"{p['kind']} {p['ref']} (id={p['id']})"
            ),
        )

    _common.run(_do())


def cmd_catalog_revoke(
    ref: str = typer.Argument(
        ...,
        metavar="ID_ИЛИ_SLUG",
        help="Навык (или коллекция при --collection): id-или-slug",
    ),
    collection: bool = typer.Option(
        False,
        "--collection",
        help="Отозвать КОЛЛЕКЦИЮ вместо навыка (collections-эндпоинт)",
    ),
    company: str | None = typer.Option(
        None, "--company", help="id компании (default: активная из JWT)"
    ),
) -> None:
    """[catalog.manage] Отозвать у компании навык (или коллекцию).

    DELETE навыка принимает id-или-slug как есть; DELETE коллекции — строго
    числовой id (slug резолвится автоматически).
    """
    cfg = ClientConfig.load()
    cid = _resolve_company_id(cfg, company)
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            if collection:
                coll_id = await _resolve_collection_id(client, ref)
                await client.revoke_catalog_collection(cid, coll_id)
                revoked: dict[str, Any] = {
                    "kind": "collection",
                    "id": coll_id,
                }
            else:
                # DELETE /catalog/skills/{slug} принимает id-ИЛИ-slug — raw.
                await client.revoke_catalog_skill(cid, ref)
                revoked = {"kind": "skill", "id": ref}
        finally:
            await client.close()
        emit_data(
            {
                "event": "catalog_revoked",
                "company_id": cid,
                "ref": ref,
                **revoked,
            },
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Отозвано у компании {p['company_id']}: "
                f"{p['kind']} {p['ref']}"
            ),
        )

    _common.run(_do())


# ======================================================
#                    register
# ======================================================
def register(
    app: typer.Typer,
    *,
    can_list: bool = False,
    can_create: bool = False,
    can_edit: bool = False,
    can_invite_links: bool = False,
    can_catalog_view: bool = False,
    can_catalog_manage: bool = False,
) -> None:
    """Регистрирует sub-app ``company`` (P1 C2).

    ``show``/``switch`` — always-on для залогиненного (backend сам режет
    tenant-изоляцией). Остальное гейтится permissions (зеркало backend):

    - ``can_list`` — hub.admin (GET /companies);
    - ``can_create`` — hub.company_create;
    - ``can_edit`` — company.manage | hub.admin;
    - ``can_invite_links`` — company.manage | role.manage | hub.admin
      (фактический гейт backend ``_require_company_admin``);
    - ``can_catalog_view`` — catalog.manage | catalog.view_all | hub.admin;
    - ``can_catalog_manage`` — catalog.manage | hub.admin (grant/revoke).
    """
    company_app = typer.Typer(
        no_args_is_help=True, help="Компании: CRUD, invite-links, каталог (P1)"
    )
    company_app.command("show")(cmd_company_show)
    company_app.command("switch")(cmd_company_switch)
    if can_list:
        company_app.command("list")(cmd_company_list)
    if can_create:
        company_app.command("create")(cmd_company_create)
    if can_edit:
        company_app.command("edit")(cmd_company_edit)
    if can_invite_links:
        il_app = typer.Typer(
            no_args_is_help=True,
            help="Переиспользуемые пригласительные ссылки компании",
        )
        il_app.command("list")(cmd_invite_links_list)
        il_app.command("create")(cmd_invite_links_create)
        il_app.command("revoke")(cmd_invite_links_revoke)
        company_app.add_typer(il_app, name="invite-links")
    if can_catalog_view or can_catalog_manage:
        cat_app = typer.Typer(
            no_args_is_help=True, help="Granted-каталог компании"
        )
        cat_app.command("list")(cmd_catalog_list)
        if can_catalog_manage:
            cat_app.command("grant")(cmd_catalog_grant)
            cat_app.command("revoke")(cmd_catalog_revoke)
        company_app.add_typer(cat_app, name="catalog")
    app.add_typer(company_app, name="company")
