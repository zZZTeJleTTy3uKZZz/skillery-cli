"""``skills-hub collection …`` — E10 коллекции (серверные + локальные).

Единый sub-app ``collection`` с глаголами ``list / show / install / create /
add / remove / delete``. Флаг **``--local``** переключает источник:

- без ``--local`` — **серверная** коллекция хаба (gate ``skill.read`` /
  ``skill.install``; CRUD серверных коллекций — через Web UI, в CLI только
  ``list`` / ``show`` / ``install``);
- с ``--local`` — **локальная** коллекция (``<config_dir>/collections.toml``,
  см. ``core/local_collections.py``) — полностью оффлайн, без логина и хаба.

``create / add / remove / delete`` существуют ТОЛЬКО в локальном режиме —
требуют ``--local`` (серверный CRUD идёт через Web UI).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core import local_collections, project_manifest
from skills_hub_cli.daemon.instrumentation import track_skill_event
from skills_hub_cli.output import emit_data, emit_error, emit_message

console = Console()

# Lazy-биндинг install-helper'ов из ``__main__`` (импорт ``__main__`` на верхнем
# уровне триггерит ``build_app()`` — подтягиваем по требованию). Тесты
# монкипатчат ``collection._install_chain`` напрямую; placeholder ниже это
# позволяет (если None — резолвим из ``__main__``).
_install_chain = None  # type: ignore[assignment]
_resolve_install_scope = None  # type: ignore[assignment]

# Гейтинг серверного режима — выставляется в ``register`` по RBAC-правам JWT
# (как у member/company sub-app). Локальный режим (``--local``) от них не зависит.
_SERVER_ENABLED = False
_CAN_INSTALL = False
# D-CLI M-2: серверный CRUD коллекций (create/add/remove/delete/tags) — право
# ``catalog.manage`` (или hub.admin). Локальный режим от него не зависит.
_CAN_MANAGE = False


def _ensure_install_helpers() -> None:
    """Подтянуть ``_install_chain`` / ``_resolve_install_scope`` из ``__main__``.

    Не перезаписывает уже выставленные (в т.ч. monkeypatch'ем) значения.
    """
    global _install_chain, _resolve_install_scope
    if _install_chain is None or _resolve_install_scope is None:
        from skills_hub_cli import __main__ as _main

        if _install_chain is None:
            _install_chain = _main._install_chain
        if _resolve_install_scope is None:
            _resolve_install_scope = _main._resolve_install_scope


def _emit_local_error(e: local_collections.LocalCollectionError) -> None:
    """LocalCollectionError → единый контракт ошибок CLI (emit_error + exit 1)."""
    emit_error(e.code, e.message)
    raise typer.Exit(1) from None


def _require_server(verb: str) -> None:
    """Серверный режим доступен только при skill.read (+install для install)."""
    if not _SERVER_ENABLED:
        emit_error(
            "NOT_AVAILABLE",
            f"Серверные коллекции недоступны без логина (нужно право skill.read). "
            f"Для локальной коллекции добавьте --local: "
            f"skillery collection {verb} … --local",
        )
        raise typer.Exit(1)


def _require_manage(verb: str) -> None:
    """Серверный CRUD коллекций (M-2) гейтится правом ``catalog.manage``."""
    if not _CAN_MANAGE:
        emit_error(
            "NOT_AVAILABLE",
            f"Управление серверными коллекциями требует право catalog.manage. "
            f"Для локальной коллекции добавьте --local: "
            f"skillery collection {verb} … --local.",
        )
        raise typer.Exit(1)


# ======================================================
#  list
# ======================================================
def _list_server(
    company_id: str | None,
    type_: str | None,
    owner_id: str | None,
    include_global: bool,
    page: int | None = None,
    size: int | None = None,
    sort: str | None = None,
    q: str | None = None,
) -> None:
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_collections(
                company_id=company_id,
                type=type_,
                owner_id=owner_id,
                include_global=include_global,
                page=page,
                size=size,
                sort=sort,
                q=q,
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            items = p.get("items") or []
            total = p.get("total")
            if total is not None:
                cur_size = p.get("size") or len(items) or 1
                pages = (total + cur_size - 1) // max(cur_size, 1)
                title = (
                    f"Collections (всего: {total}, "
                    f"стр. {p.get('page') or 1}/{max(pages, 1)})"
                )
            else:
                title = f"Collections (count={len(items)})"
            table = Table(title=title)
            for col in ("slug", "title", "type", "skills", "owner", "company"):
                table.add_column(col, overflow="fold" if col == "title" else None)
            for c in items:
                owner = c.get("owner") or {}
                company = c.get("company") or {}
                table.add_row(
                    c.get("slug", ""),
                    c.get("title", ""),
                    c.get("type", ""),
                    str(c.get("skills_count", 0)),
                    (owner.get("display_name") or owner.get("email") or "—"),
                    (company.get("name") or company.get("slug") or "—"),
                )
            if not items:
                console.print("[yellow]Коллекций нет[/]")
            else:
                console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def _list_local() -> None:
    cfg = ClientConfig.load()
    store = cfg.effective_store_dir()
    try:
        colls = local_collections.load_all()
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)
    items = [
        {
            "name": n,
            "title": b["title"],
            "skills_count": len(b["skills"]),
            "skills": b["skills"],
            "missing_in_store": local_collections.missing_in_store(b["skills"], store),
        }
        for n, b in sorted(colls.items())
    ]

    def _render(rows: list) -> None:
        if not rows:
            console.print(
                "[yellow]Локальных коллекций нет[/] "
                f"[dim]({local_collections.collections_path()})[/]"
            )
            return
        table = Table(title="Локальные коллекции")
        table.add_column("name")
        table.add_column("title", overflow="fold")
        table.add_column("skills")
        table.add_column("нет в сторе", overflow="fold")
        for r in rows:
            table.add_row(
                r["name"], r["title"], str(r["skills_count"]),
                ", ".join(r["missing_in_store"]) or "—",
            )
        console.print(table)

    emit_data(items, text_renderer=_render)


def cmd_collection_list(
    local: bool = typer.Option(
        False, "--local",
        help="Локальные коллекции (collections.toml), а не серверный каталог.",
    ),
    company_id: str | None = typer.Option(None, "--company"),
    type_: str | None = typer.Option(None, "--type", help="static | dynamic"),
    owner_id: str | None = typer.Option(None, "--owner"),
    include_global: bool = typer.Option(
        True, "--include-global/--no-global",
        help="Включать global-коллекции (default: yes).",
    ),
    page: int = typer.Option(1, "--page", min=1, help="Номер страницы (1-based)"),
    size: int | None = typer.Option(
        None, "--size", help="Размер страницы (default backend)"
    ),
    sort: str | None = typer.Option(
        None, "--sort", help="title | created | updated"
    ),
    q: str | None = typer.Option(
        None, "--q", help="Поиск по названию/slug (подстрока)"
    ),
) -> None:
    """Список коллекций: серверный каталог (default) или ``--local``.

    M-6: серверный режим поддерживает offset-пагинацию (``--page``/``--size``/
    ``--sort``/``--q``). В ``--local`` эти опции игнорируются.
    """
    if local:
        _list_local()
        return
    _require_server("list")
    _list_server(
        company_id, type_, owner_id, include_global,
        page=page, size=size, sort=sort, q=q,
    )


# ======================================================
#  show
# ======================================================
def _show_server(ref: str) -> None:
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_collection(ref)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            c = p["collection"]
            console.print(f"[bold]{c['title']}[/] ({c['slug']})")
            console.print(
                f"  type:       {c['type']}   "
                f"skills_count: {c.get('skills_count', 0)}"
            )
            if c.get("description"):
                console.print(f"  desc:       {c['description']}")
            owner = c.get("owner") or {}
            console.print(
                f"  owner:      {owner.get('display_name') or owner.get('email') or '—'}"
            )
            comp = c.get("company") or {}
            console.print(f"  company:    {comp.get('name') or '—'}")
            tags = p.get("tags") or []
            if tags:
                tag_labels = [t.get("label") or t.get("slug") for t in tags]
                console.print(f"  tags:       {', '.join(tag_labels)}")
            skills = p.get("skills") or []
            if skills:
                console.print("  skills:")
                for s in skills:
                    console.print(f"    • {s.get('slug'):20} — {s.get('title')}")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def _show_local(name: str) -> None:
    cfg = ClientConfig.load()
    try:
        coll = local_collections.get(name)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)
    if coll is None:
        emit_error("NOT_FOUND", f"Локальная коллекция «{name}» не найдена")
        raise typer.Exit(1)
    missing = local_collections.missing_in_store(
        coll["skills"], cfg.effective_store_dir()
    )

    def _render(p: dict[str, Any]) -> None:
        console.print(f"[bold]{p['title']}[/] ({p['name']}) [dim]— локальная[/]")
        console.print(f"  skills ({len(p['skills'])}): {', '.join(p['skills']) or '—'}")
        if p["missing_in_store"]:
            console.print(
                f"  [yellow]нет в сторе:[/] {', '.join(p['missing_in_store'])}"
            )

    emit_data(
        {
            "name": name, "title": coll["title"], "skills": coll["skills"],
            "missing_in_store": missing,
        },
        text_renderer=_render,
    )


def cmd_collection_show(
    ref: str = typer.Argument(
        ..., metavar="ID_SLUG_ИЛИ_ИМЯ",
        help="id-или-slug серверной коллекции, либо имя локальной (--local)",
    ),
    local: bool = typer.Option(
        False, "--local", help="Показать локальную коллекцию из collections.toml."
    ),
) -> None:
    """Детали коллекции: серверной (default) или локальной (``--local``)."""
    if local:
        _show_local(ref)
        return
    _require_server("show")
    _show_server(ref)


# ======================================================
#  install
# ======================================================
def _install_server(
    ref: str, scope: str | None, project: Path | None, channel: str,
    force: bool, agent: str | None,
) -> None:
    from skills_hub_cli.core.agents import get_target

    _ensure_install_helpers()
    cfg = ClientConfig.load()
    actual_scope, project_path = _resolve_install_scope(cfg, scope, project)
    access = _common.get_access_token()
    target = get_target(agent or cfg.agent)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            detail = await client.get_collection(ref)
            coll = detail["collection"]
            skills = detail.get("skills") or []
            installed: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            for s in skills:
                skill_ref = s.get("slug") or str(s.get("id") or "")
                if not skill_ref:
                    continue
                try:
                    chain = await _install_chain(
                        cfg, access, slug=skill_ref, channel=channel,
                        scope=actual_scope, project_path=project_path,
                        force=force, agent_target=target,
                    )
                    installed.extend(chain)
                except Exception as exc:
                    failed.append({"ref": skill_ref, "error": str(exc)})
        finally:
            await client.close()

        payload: dict[str, Any] = {
            "event": "collection_installed",
            "collection": {
                "id": coll.get("id"), "slug": coll.get("slug"),
                "title": coll.get("title"),
            },
            "scope": actual_scope,
            "project": str(project_path) if project_path else None,
            "installed": installed, "failed": failed,
            "skills_total": len(skills),
        }

        def _render(p: dict[str, Any]) -> None:
            c = p["collection"]
            console.print(
                f"[bold]Коллекция {c.get('title')}[/] "
                f"({c.get('slug') or c.get('id')}) — навыков: {p['skills_total']}"
            )
            for item in p["installed"]:
                action = "Обновлён" if item.get("is_update") else "Установлен"
                mount = "📎" if item.get("linked") else "📄"
                label = item.get("slug") or item.get("skill_id")
                console.print(
                    f"  [green]✓[/] {action} ({item.get('scope')}) {mount} "
                    f"{label}@{item.get('version')} → {item.get('target_dir')}"
                )
            for f in p["failed"]:
                console.print(f"  [red]✗[/] {f['ref']}: {f['error']}")
            if not p["installed"] and not p["failed"]:
                console.print("[yellow]В коллекции нет навыков для установки[/]")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def _install_local(
    name: str, scope: str | None, project: Path | None, channel: str,
    force: bool, agent: str | None,
) -> None:
    from skills_hub_cli.core.agents import get_target
    from skills_hub_cli.core.installer import SkillInstaller, read_meta

    _ensure_install_helpers()
    cfg = ClientConfig.load()
    try:
        coll = local_collections.get(name)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)
    if coll is None:
        emit_error("NOT_FOUND", f"Локальная коллекция «{name}» не найдена")
        raise typer.Exit(1)
    actual_scope, project_path = _resolve_install_scope(cfg, scope, project)
    target = get_target(agent or cfg.agent)
    store_dir = cfg.effective_store_dir()
    installer = SkillInstaller(target, store_dir)

    linked: list[dict[str, Any]] = []
    installed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    async def _do() -> None:
        access_holder: dict[str, str] = {}
        for slug in coll["skills"]:
            # 1) Уже в сторе → локальный re-link (БЕЗ сети и логина).
            try:
                local = installer.link_existing(slug, project=project_path, force=force)
            except RuntimeError as exc:  # чужая папка без meta и без --force
                skipped.append({"slug": slug, "reason": str(exc)})
                continue
            if local is not None:
                was_linked, link_kind = local
                meta = read_meta(store_dir / slug) or {}
                if project_path is not None:
                    project_manifest.add(project_path, slug)
                # Навык уже материализован в сторе → re-link = включение-в-
                # проект → skill.enable (НЕ повторный skill.install). source
                # берём из meta стора (откуда навык пришёл изначально).
                if actual_scope == "project":
                    track_skill_event(
                        "skill.enable", slug=slug,
                        version=meta.get("version") or "", scope="project",
                        source=meta.get("source"), agent=target.name,
                    )
                linked.append({
                    "slug": meta.get("slug") or slug,
                    "skill_id": meta.get("skill_id"),
                    "version": meta.get("version"),
                    "target_dir": str(target.slug_dir(slug, project=project_path)),
                    "scope": actual_scope, "linked": was_linked,
                    "link_kind": link_kind, "source": "store",
                })
                continue

            # 2) В сторе нет → hub-докачка (только если залогинен).
            if "denied" in access_holder:
                skipped.append({"slug": slug, "reason": access_holder["denied"]})
                continue
            if not cfg.is_logged_in():
                access_holder["denied"] = (
                    "нет в сторе; вы не залогинены — докачка из хаба недоступна"
                )
                skipped.append({"slug": slug, "reason": access_holder["denied"]})
                continue
            if "tok" not in access_holder:
                try:
                    access_holder["tok"] = _common.get_access_token()
                except typer.Exit:
                    access_holder["denied"] = (
                        "нет в сторе; access-токен не найден — сделайте login заново"
                    )
                    skipped.append({"slug": slug, "reason": access_holder["denied"]})
                    continue
            try:
                chain = await _install_chain(
                    cfg, access_holder["tok"], slug=slug, channel=channel,
                    scope=actual_scope, project_path=project_path,
                    force=force, agent_target=target,
                )
            except Exception as exc:
                skipped.append({"slug": slug, "reason": str(exc)})
                continue
            if project_path is not None:
                for item in chain:
                    ref = item.get("slug") or item.get("skill_id")
                    if ref:
                        project_manifest.add(project_path, str(ref))
            installed.extend(chain)

        payload: dict[str, Any] = {
            "event": "local_collection_installed",
            "name": name, "title": coll["title"], "scope": actual_scope,
            "project": str(project_path) if project_path else None,
            "installed": installed, "linked": linked, "skipped": skipped,
            "skills_total": len(coll["skills"]),
        }
        if skipped and not cfg.is_logged_in():
            payload["hint"] = (
                "вы не залогинены — недостающие в сторе навыки пропущены; "
                "для докачки из хаба: skillery login"
            )

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[bold]Локальная коллекция «{p['title']}»[/] ({p['name']}) — "
                f"навыков: {p['skills_total']}"
            )
            for item in p["linked"]:
                mount = "📎" if item.get("linked") else "📄"
                console.print(
                    f"  [green]✓[/] Включён ({item.get('scope')}) {mount} "
                    f"{item.get('slug')}@{item.get('version')} → "
                    f"{item.get('target_dir')} [dim](из стора, без сети)[/]"
                )
            for item in p["installed"]:
                action = "Обновлён" if item.get("is_update") else "Установлен"
                mount = "📎" if item.get("linked") else "📄"
                label = item.get("slug") or item.get("skill_id")
                console.print(
                    f"  [green]✓[/] {action} ({item.get('scope')}) {mount} "
                    f"{label}@{item.get('version')} → {item.get('target_dir')}"
                )
            for s in p["skipped"]:
                console.print(f"  [yellow]→ Пропущен[/] {s['slug']}: {s['reason']}")
            if p.get("hint"):
                console.print(f"  [dim]{p['hint']}[/]")
            if not coll["skills"]:
                console.print("[yellow]Коллекция пуста[/]")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_collection_install(
    ref: str = typer.Argument(
        ..., metavar="ID_SLUG_ИЛИ_ИМЯ",
        help="id-или-slug серверной коллекции, либо имя локальной (--local)",
    ),
    local: bool = typer.Option(
        False, "--local", help="Установить ЛОКАЛЬНУЮ коллекцию (без хаба)."
    ),
    scope: str | None = typer.Option(
        None, "--scope",
        help="global | project (default из config.default_install_scope)",
    ),
    project: Path | None = typer.Option(
        None, "--project",
        help="Если scope=project — путь к корню проекта (default: cwd)",
    ),
    channel: str = typer.Option("published", "--channel"),
    force: bool = typer.Option(
        False, "--force", help="Перезаписать существующие папки навыков"
    ),
    agent: str | None = typer.Option(None, "--agent"),
) -> None:
    """Массово установить навыки коллекции (серверной или ``--local``).

    Серверная: тянет effective skills через ``GET /collections/{slug}`` и ставит
    каждый через общий ``_install_chain``. Локальная: для каждого слага — линк из
    стора (без сети) или докачка из хаба (если залогинен).
    """
    if local:
        _install_local(ref, scope, project, channel, force, agent)
        return
    _require_server("install")
    if not _CAN_INSTALL:
        emit_error(
            "NOT_AVAILABLE",
            "Установка серверной коллекции требует право skill.install. "
            "Для локальной коллекции добавьте --local.",
        )
        raise typer.Exit(1)
    _install_server(ref, scope, project, channel, force, agent)


# ======================================================
#  create / add / remove / delete — ТОЛЬКО локальные (--local)
# ======================================================
def _create_server(
    name: str,
    title: str | None,
    type_: str,
    description: str | None,
    company: str | None,
) -> None:
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            r = await client.create_collection(
                title=title or name,
                type=type_,
                slug=name,
                description=description,
                company_id=company,
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            label = p.get("slug") or p.get("id")
            console.print(
                f"[green]✓[/] Серверная коллекция «{p.get('title')}» "
                f"создана ({label}, type={p.get('type')})"
            )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_collection_create(
    name: str = typer.Argument(
        ..., help="Имя/slug коллекции (буквы/цифры/«-»/«_»)"
    ),
    local: bool = typer.Option(
        False, "--local", help="Создать ЛОКАЛЬНУЮ коллекцию (оффлайн, без хаба)."
    ),
    title: str | None = typer.Option(
        None, "--title", help="Человекочитаемый заголовок (default: имя)"
    ),
    type_: str = typer.Option(
        "static", "--type", help="static | dynamic (только серверная)"
    ),
    description: str | None = typer.Option(
        None, "--description", help="Описание (только серверная)"
    ),
    company: str | None = typer.Option(
        None, "--company", help="ID компании (только серверная; None ⇒ global)"
    ),
) -> None:
    """Создать коллекцию: серверную (catalog.manage) или ``--local``.

    M-2: без ``--local`` создаётся СЕРВЕРНАЯ коллекция через POST /collections
    (право catalog.manage). С ``--local`` — локальная в collections.toml.
    """
    if not local:
        _require_manage("create")
        _create_server(name, title, type_, description, company)
        return
    try:
        coll = local_collections.create(name, title=title)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)

    emit_data(
        {
            "event": "local_collection_created", "name": name,
            "title": coll["title"], "skills": coll["skills"],
            "path": str(local_collections.collections_path()),
        },
        text_renderer=lambda p: console.print(
            f"[green]✓[/] Локальная коллекция «{p['name']}» создана "
            f"[dim]({p['path']})[/]"
        ),
    )


def cmd_collection_delete(
    name: str = typer.Argument(..., help="Имя локальной коллекции"),
    local: bool = typer.Option(
        False, "--local", help="Обязателен: удаляется только локальная коллекция."
    ),
) -> None:
    """Удалить ЛОКАЛЬНУЮ коллекцию (требует ``--local``; навыки на диске целы).

    Удаление СЕРВЕРНОЙ коллекции из CLI не поддержано (только через Web UI) —
    в CLI доступны create/add/remove/tags серверных коллекций, но не delete.
    """
    if not local:
        emit_error(
            "USE_LOCAL_FLAG",
            "Удаление серверной коллекции из CLI не поддержано (через Web UI). "
            "Для локальной коллекции добавьте --local: "
            "skillery collection delete … --local.",
        )
        raise typer.Exit(1)
    try:
        local_collections.delete(name)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)
    emit_data(
        {"event": "local_collection_deleted", "name": name},
        text_renderer=lambda p: console.print(
            f"[green]✓[/] Локальная коллекция «{p['name']}» удалена "
            "[dim](навыки на диске не тронуты)[/]"
        ),
    )


def _add_server(name: str, skill_ref: str) -> None:
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = await _common.resolve_skill_id(client, skill_ref)
            r = await client.add_skill_to_collection(name, skill_id)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Навык {skill_ref} (id={p.get('skill_id')}) "
                f"добавлен в коллекцию «{p.get('collection_slug') or name}»"
            )

        emit_data(r, text_renderer=_render)

    _common.run(_do())


def cmd_collection_add(
    name: str = typer.Argument(..., help="Имя/slug коллекции"),
    skill_slug: str = typer.Argument(
        ..., metavar="SKILL_SLUG", help="Слаг навыка (или числовой id)"
    ),
    local: bool = typer.Option(
        False, "--local", help="Править ЛОКАЛЬНУЮ коллекцию (а не серверную)."
    ),
) -> None:
    """Добавить навык в коллекцию: серверную (catalog.manage) или ``--local``.

    M-2: без ``--local`` — POST /collections/{slug}/skills (slug навыка
    резолвится в числовой id). С ``--local``: если слага нет в локальном сторе
    — warning, но слаг добавляется (``install --local`` докачает из хаба).
    """
    if not local:
        _require_manage("add")
        _add_server(name, skill_slug)
        return
    cfg = ClientConfig.load()
    try:
        coll, added = local_collections.add_skill(name, skill_slug)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)
    in_store = not local_collections.missing_in_store(
        [skill_slug], cfg.effective_store_dir()
    )
    if not in_store:
        emit_message(
            f"«{skill_slug}» нет в локальном сторе — добавлен в коллекцию; "
            "install --local докачает его из хаба (нужен login).",
            level="warn",
        )

    def _render(p: dict[str, Any]) -> None:
        if p["added"]:
            console.print(f"[green]✓[/] «{p['skill']}» добавлен в «{p['name']}»")
        else:
            console.print(f"[dim]«{p['skill']}» уже в «{p['name']}»[/]")
        console.print(f"  Состав ({len(p['skills'])}): {', '.join(p['skills'])}")

    emit_data(
        {
            "event": "local_collection_skill_added", "name": name,
            "skill": skill_slug, "added": added, "in_store": in_store,
            "skills": coll["skills"],
        },
        text_renderer=_render,
    )


def _remove_server(name: str, skill_ref: str) -> None:
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            # DELETE /collections/{slug}/skills/{skill_id} принимает id-или-slug
            # навыка как есть (backend резолвит), idempotent.
            await client.remove_skill_from_collection(name, skill_ref)
        finally:
            await client.close()
        emit_data(
            {
                "event": "collection_skill_removed",
                "collection": name,
                "skill": skill_ref,
            },
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Навык {p['skill']} убран из коллекции "
                f"«{p['collection']}»"
            ),
        )

    _common.run(_do())


def cmd_collection_remove(
    name: str = typer.Argument(..., help="Имя/slug коллекции"),
    skill_slug: str = typer.Argument(
        ..., metavar="SKILL_SLUG", help="Слаг навыка (или числовой id)"
    ),
    local: bool = typer.Option(
        False, "--local", help="Править ЛОКАЛЬНУЮ коллекцию (а не серверную)."
    ),
) -> None:
    """Убрать навык из коллекции: серверной (catalog.manage) или ``--local``.

    M-2: без ``--local`` — DELETE /collections/{slug}/skills/{skill} (idempotent).
    С ``--local`` — правка collections.toml (навыки на диске целы).
    """
    if not local:
        _require_manage("remove")
        _remove_server(name, skill_slug)
        return
    try:
        coll, removed = local_collections.remove_skill(name, skill_slug)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)

    def _render(p: dict[str, Any]) -> None:
        if p["removed"]:
            console.print(f"[green]✓[/] «{p['skill']}» убран из «{p['name']}»")
        else:
            console.print(f"[yellow]«{p['skill']}» не было в «{p['name']}»[/]")

    emit_data(
        {
            "event": "local_collection_skill_removed", "name": name,
            "skill": skill_slug, "removed": removed, "skills": coll["skills"],
        },
        text_renderer=_render,
    )


def cmd_collection_tags(
    name: str = typer.Argument(..., help="slug серверной коллекции"),
    tag_ids: str = typer.Option(
        ...,
        "--tags",
        help="ID тегов через запятую (replace-set; пусто = очистить)",
    ),
) -> None:
    """Задать набор тегов СЕРВЕРНОЙ коллекции (PUT /collections/{slug}/tags).

    M-2: replace-set — полная замена набора тегов. Право catalog.manage.
    ``--tags`` принимает строго ЧИСЛОВЫЕ id через запятую (бэк 422 на нечисловые);
    ``--tags ""`` очищает все теги. Серверная операция (нет ``--local``).
    """
    _require_manage("tags")
    ids = [t.strip() for t in tag_ids.split(",") if t.strip()]
    bad = [t for t in ids if not t.isdigit()]
    if bad:
        emit_error(
            "VALIDATION",
            f"--tags должны быть числовыми id, получено: {', '.join(bad)}",
        )
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.set_collection_tags(name, tag_ids=ids)
        finally:
            await client.close()
        emit_data(
            {"event": "collection_tags_set", "collection": name, "tag_ids": ids},
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Теги коллекции «{p['collection']}» обновлены: "
                f"{', '.join(p['tag_ids']) or '(очищено)'}"
            ),
        )

    _common.run(_do())


def register(
    app: typer.Typer,
    *,
    server_enabled: bool = False,
    can_install: bool = False,
    can_manage: bool = False,
) -> None:
    """Регистрация единого ``collection`` sub-app.

    Вызывается ВСЕГДА (из always-on зоны ``build_app``). Локальный режим
    (``--local``) работает без логина; серверный — гейтится ``server_enabled``
    (``skill.read``), ``can_install`` (``skill.install``) и ``can_manage``
    (``catalog.manage`` — серверный CRUD, M-2), которые прокидываются в
    модульные флаги и проверяются командами в рантайме.
    """
    global _SERVER_ENABLED, _CAN_INSTALL, _CAN_MANAGE
    _SERVER_ENABLED = server_enabled
    _CAN_INSTALL = can_install
    _CAN_MANAGE = can_manage

    collection_app = typer.Typer(
        no_args_is_help=True,
        help="Коллекции навыков: серверные (хаб) + локальные (--local)",
    )
    collection_app.command("list")(cmd_collection_list)
    collection_app.command("show")(cmd_collection_show)
    collection_app.command("install")(cmd_collection_install)
    collection_app.command("create")(cmd_collection_create)
    collection_app.command("add")(cmd_collection_add)
    collection_app.command("remove")(cmd_collection_remove)
    collection_app.command("delete")(cmd_collection_delete)
    # M-2: tags — серверная операция (replace-set тегов коллекции).
    collection_app.command("tags")(cmd_collection_tags)
    app.add_typer(collection_app, name="collection")
