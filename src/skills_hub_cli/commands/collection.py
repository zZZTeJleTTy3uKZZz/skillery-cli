"""``skills-hub collections`` / ``skills-hub collection show`` — E10.

Read-only из CLI: list + detail. CRUD (create/delete/edit) идёт через
Web UI; для CLI это редко нужно и тестируется отдельно при
необходимости.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.output import emit_data

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


def register(app: typer.Typer, *, can_install: bool = False) -> None:
    """Register ``collections list`` + ``collection show`` (+ ``install``).

    ``can_install`` (gate ``skill.install``) включает под-команду
    ``collection install`` — массовую установку навыков коллекции. По
    умолчанию False (read-only набор), чтобы 20+ существующих вызовов
    ``register(app)`` без аргумента остались корректны.
    """
    collections_app = typer.Typer(
        no_args_is_help=True, help="Skill collections (E10)"
    )
    collections_app.command("list")(cmd_collections_list)
    app.add_typer(collections_app, name="collections")

    collection_app = typer.Typer(
        no_args_is_help=True, help="Single collection"
    )
    collection_app.command("show")(cmd_collection_show)
    if can_install:
        collection_app.command("install")(cmd_collection_install)
    app.add_typer(collection_app, name="collection")
