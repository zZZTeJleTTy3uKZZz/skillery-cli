"""``skills-hub collections`` / ``skills-hub collection …`` — E10.

Серверные команды (read-only list + detail + bulk-install) гейтятся правами
``skill.read`` / ``skill.install``; CRUD серверных коллекций идёт через Web UI.

P1 C4 — ЛОКАЛЬНЫЕ коллекции (``*-local``): личные наборы слагов в
``<config_dir>/collections.toml`` (см. ``core/local_collections.py``) —
работают полностью оффлайн, без логина и хаба; ``install-local`` линкует
навыки из стора и докачивает недостающее из хаба, если залогинен.
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
# уровне триггерит ``build_app()`` — поэтому подтягиваем по требованию). Тесты
# монкипатчат ``collection._install_chain`` напрямую; placeholder ниже
# позволяет это (если None — резолвим из ``__main__``).
_install_chain = None  # type: ignore[assignment]
_resolve_install_scope = None  # type: ignore[assignment]


def _ensure_install_helpers() -> None:
    """Подтянуть ``_install_chain`` / ``_resolve_install_scope`` из ``__main__``.

    Не перезаписывает уже выставленные (в т.ч. monkeypatch'ем в тестах)
    значения — иначе сломали бы подмену.
    """
    global _install_chain, _resolve_install_scope
    if _install_chain is None or _resolve_install_scope is None:
        from skills_hub_cli import __main__ as _main

        if _install_chain is None:
            _install_chain = _main._install_chain
        if _resolve_install_scope is None:
            _resolve_install_scope = _main._resolve_install_scope


def cmd_collections_list(
    company_id: str | None = typer.Option(None, "--company"),
    type_: str | None = typer.Option(
        None, "--type", help="static | dynamic"
    ),
    owner_id: str | None = typer.Option(None, "--owner"),
    include_global: bool = typer.Option(
        True, "--include-global/--no-global",
        help="Включать global-коллекции (default: yes).",
    ),
) -> None:
    """List коллекции (visibility scope: своя компания + global)."""
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
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            items = p.get("items") or []
            table = Table(title=f"Collections (count={len(items)})")
            table.add_column("slug")
            table.add_column("title", overflow="fold")
            table.add_column("type")
            table.add_column("skills")
            table.add_column("owner")
            table.add_column("company")
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


def cmd_collection_show(
    slug: str = typer.Argument(
        ...,
        metavar="ID_ИЛИ_SLUG",
        help="id-или-slug коллекции (backend принимает оба)",
    ),
) -> None:
    """Detail + effective skills (по id-или-slug)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_collection(slug)
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
            console.print(f"  owner:      {owner.get('display_name') or owner.get('email') or '—'}")
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
                    console.print(
                        f"    • {s.get('slug'):20} — {s.get('title')}"
                    )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_collection_install(
    slug: str = typer.Argument(
        ...,
        metavar="ID_ИЛИ_SLUG",
        help="id-или-slug коллекции (backend принимает оба)",
    ),
    scope: str | None = typer.Option(
        None,
        "--scope",
        help="global | project (default из config.default_install_scope)",
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        help="Если scope=project — путь к корню проекта (default: cwd)",
    ),
    channel: str = typer.Option("published", "--channel"),
    force: bool = typer.Option(
        False, "--force", help="Перезаписать существующие папки навыков"
    ),
    agent: str | None = typer.Option(None, "--agent"),
) -> None:
    """Массово установить ВСЕ effective skills коллекции (онбординг-кейс).

    Тянет состав коллекции через ``GET /collections/{slug}`` (effective skills)
    и ставит каждый навык тем же путём, что и ``skills-hub install`` — через
    общий ``_install_chain`` (стор + ссылка + deps). Уважает ``--scope``
    (global | project) и ``--channel``. Gate как у ``install``: ``skill.install``.

    Slug-less навык (PK migration) ставится по числовому id.
    """
    from skills_hub_cli.core.agents import get_target

    _ensure_install_helpers()
    cfg = ClientConfig.load()
    actual_scope, project_path = _resolve_install_scope(cfg, scope, project)
    _ = actual_scope  # передаётся через project_path
    access = _common.get_access_token()
    target = get_target(agent or cfg.agent)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            detail = await client.get_collection(slug)
            coll = detail["collection"]
            skills = detail.get("skills") or []
            installed: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            for s in skills:
                # id-или-slug, как принимает install (slug ?? числовой id).
                ref = s.get("slug") or str(s.get("id") or "")
                if not ref:
                    continue
                try:
                    chain = await _install_chain(
                        cfg,
                        access,
                        slug=ref,
                        channel=channel,
                        scope=actual_scope,
                        project_path=project_path,
                        force=force,
                        agent_target=target,
                    )
                    installed.extend(chain)
                except Exception as exc:  # noqa: BLE001 — копим ошибки, не падаем на первой
                    failed.append({"ref": ref, "error": str(exc)})
        finally:
            await client.close()

        payload: dict[str, Any] = {
            "event": "collection_installed",
            "collection": {
                "id": coll.get("id"),
                "slug": coll.get("slug"),
                "title": coll.get("title"),
            },
            "scope": actual_scope,
            "project": str(project_path) if project_path else None,
            "installed": installed,
            "failed": failed,
            "skills_total": len(skills),
        }

        def _render(p: dict[str, Any]) -> None:
            c = p["collection"]
            console.print(
                f"[bold]Коллекция {c.get('title')}[/] "
                f"({c.get('slug') or c.get('id')}) — "
                f"навыков: {p['skills_total']}"
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


# ======================================================
#  P1 C4 — локальные коллекции (оффлайн, без хаба)
# ======================================================
def _emit_local_error(e: local_collections.LocalCollectionError) -> None:
    """LocalCollectionError → единый контракт ошибок CLI (emit_error + exit 1)."""
    emit_error(e.code, e.message)
    raise typer.Exit(1) from None


def cmd_collection_create_local(
    name: str = typer.Argument(
        ..., help="Имя локальной коллекции (буквы/цифры/«-»/«_»)"
    ),
    title: str | None = typer.Option(
        None, "--title", help="Человекочитаемый заголовок (default: имя)"
    ),
) -> None:
    """Создать локальную коллекцию (оффлайн, без хаба и логина)."""
    try:
        coll = local_collections.create(name, title=title)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)

    def _render(p: dict[str, Any]) -> None:
        console.print(
            f"[green]✓[/] Локальная коллекция «{p['name']}» создана "
            f"[dim]({p['path']})[/]"
        )

    emit_data(
        {
            "event": "local_collection_created",
            "name": name,
            "title": coll["title"],
            "skills": coll["skills"],
            "path": str(local_collections.collections_path()),
        },
        text_renderer=_render,
    )


def cmd_collection_delete_local(
    name: str = typer.Argument(..., help="Имя локальной коллекции"),
) -> None:
    """Удалить локальную коллекцию (установленные навыки не трогаются)."""
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


def cmd_collection_add_local(
    name: str = typer.Argument(..., help="Имя локальной коллекции"),
    skill_slug: str = typer.Argument(
        ..., metavar="SKILL_SLUG", help="Слаг навыка (или числовой id)"
    ),
) -> None:
    """Добавить навык в локальную коллекцию.

    Если слага нет в локальном сторе — warning, но слаг добавляется всё
    равно (``install-local`` докачает его из хаба при наличии логина).
    """
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
            "install-local докачает его из хаба (нужен login).",
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
            "event": "local_collection_skill_added",
            "name": name,
            "skill": skill_slug,
            "added": added,
            "in_store": in_store,
            "skills": coll["skills"],
        },
        text_renderer=_render,
    )


def cmd_collection_remove_local(
    name: str = typer.Argument(..., help="Имя локальной коллекции"),
    skill_slug: str = typer.Argument(
        ..., metavar="SKILL_SLUG", help="Слаг навыка (или числовой id)"
    ),
) -> None:
    """Убрать навык из локальной коллекции (диск/стор не трогаются)."""
    try:
        coll, removed = local_collections.remove_skill(name, skill_slug)
    except local_collections.LocalCollectionError as e:
        _emit_local_error(e)

    def _render(p: dict[str, Any]) -> None:
        if p["removed"]:
            console.print(f"[green]✓[/] «{p['skill']}» убран из «{p['name']}»")
        else:
            console.print(
                f"[yellow]«{p['skill']}» не было в «{p['name']}»[/]"
            )

    emit_data(
        {
            "event": "local_collection_skill_removed",
            "name": name,
            "skill": skill_slug,
            "removed": removed,
            "skills": coll["skills"],
        },
        text_renderer=_render,
    )


def cmd_collection_list_local() -> None:
    """Локальные коллекции: имя, размер, какие слаги отсутствуют в сторе."""
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
            "missing_in_store": local_collections.missing_in_store(
                b["skills"], store
            ),
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
                r["name"],
                r["title"],
                str(r["skills_count"]),
                ", ".join(r["missing_in_store"]) or "—",
            )
        console.print(table)

    emit_data(items, text_renderer=_render)


def cmd_collection_install_local(
    name: str = typer.Argument(..., help="Имя локальной коллекции"),
    scope: str | None = typer.Option(
        None,
        "--scope",
        help="global | project (default из config.default_install_scope)",
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        help="Если scope=project — путь к корню проекта (default: cwd)",
    ),
    force: bool = typer.Option(
        False, "--force", help="Перезаписать существующие папки навыков"
    ),
    channel: str = typer.Option(
        "published", "--channel", help="Канал hub-докачки отсутствующих в сторе"
    ),
    agent: str | None = typer.Option(None, "--agent"),
) -> None:
    """Установить локальную коллекцию: стор → линк; недостающее — из хаба.

    Для каждого слага коллекции:
    - есть в сторе → локальный линк в scope (как ``enable``, БЕЗ сети);
    - нет в сторе и залогинен → докачка из хаба (общий ``_install_chain``);
    - нет в сторе и НЕ залогинен → skip с подсказкой (команда не падает).
    Итог JSON: ``{installed: [...], linked: [...], skipped: [...]}``.
    """
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
                local = installer.link_existing(
                    slug, project=project_path, force=force
                )
            except RuntimeError as exc:  # чужая папка без meta и без --force
                skipped.append({"slug": slug, "reason": str(exc)})
                continue
            if local is not None:
                was_linked, link_kind = local
                meta = read_meta(store_dir / slug) or {}
                if project_path is not None:
                    project_manifest.add(project_path, slug)
                track_skill_event(
                    "skill.install",
                    slug=slug,
                    version=meta.get("version") or "",
                    scope=actual_scope,
                )
                linked.append({
                    "slug": meta.get("slug") or slug,
                    "skill_id": meta.get("skill_id"),
                    "version": meta.get("version"),
                    "target_dir": str(
                        target.slug_dir(slug, project=project_path)
                    ),
                    "scope": actual_scope,
                    "linked": was_linked,
                    "link_kind": link_kind,
                    "source": "store",
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
                        "нет в сторе; access-токен не найден — "
                        "сделайте login заново"
                    )
                    skipped.append(
                        {"slug": slug, "reason": access_holder["denied"]}
                    )
                    continue
            try:
                chain = await _install_chain(
                    cfg,
                    access_holder["tok"],
                    slug=slug,
                    channel=channel,
                    scope=actual_scope,
                    project_path=project_path,
                    force=force,
                    agent_target=target,
                )
            except Exception as exc:  # noqa: BLE001 — копим ошибки, не падаем на первой
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
            "name": name,
            "title": coll["title"],
            "scope": actual_scope,
            "project": str(project_path) if project_path else None,
            "installed": installed,
            "linked": linked,
            "skipped": skipped,
            "skills_total": len(coll["skills"]),
        }
        if skipped and not cfg.is_logged_in():
            payload["hint"] = (
                "вы не залогинены — недостающие в сторе навыки пропущены; "
                "для докачки из хаба: skills-hub login"
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
                console.print(
                    f"  [yellow]→ Пропущен[/] {s['slug']}: {s['reason']}"
                )
            if p.get("hint"):
                console.print(f"  [dim]{p['hint']}[/]")
            if not coll["skills"]:
                console.print("[yellow]Коллекция пуста[/]")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def register(
    app: typer.Typer,
    *,
    server_enabled: bool = False,
    can_install: bool = False,
) -> None:
    """Регистрация ``collections`` / ``collection`` sub-app'ов.

    P1 C4: вызывается ВСЕГДА (из always-on зоны ``build_app``), гейтинг —
    флагами внутри модуля:

    - локальные подкоманды (``create-local`` / ``delete-local`` /
      ``add-local`` / ``remove-local`` / ``list-local`` / ``install-local``)
      регистрируются БЕЗУСЛОВНО — работают оффлайн без логина;
    - серверные ``collections list`` + ``collection show`` — только при
      ``server_enabled`` (gate ``skill.read``);
    - ``collection install`` — при ``server_enabled`` И ``can_install``
      (gate ``skill.install``).
    """
    if server_enabled:
        collections_app = typer.Typer(
            no_args_is_help=True, help="Skill collections (E10)"
        )
        collections_app.command("list")(cmd_collections_list)
        app.add_typer(collections_app, name="collections")

    collection_app = typer.Typer(
        no_args_is_help=True, help="Single collection (server + local)"
    )
    if server_enabled:
        collection_app.command("show")(cmd_collection_show)
        if can_install:
            collection_app.command("install")(cmd_collection_install)
    # --- P1 local-collections: оффлайн-команды, всегда доступны ---
    collection_app.command("create-local")(cmd_collection_create_local)
    collection_app.command("delete-local")(cmd_collection_delete_local)
    collection_app.command("add-local")(cmd_collection_add_local)
    collection_app.command("remove-local")(cmd_collection_remove_local)
    collection_app.command("list-local")(cmd_collection_list_local)
    collection_app.command("install-local")(cmd_collection_install_local)
    app.add_typer(collection_app, name="collection")
