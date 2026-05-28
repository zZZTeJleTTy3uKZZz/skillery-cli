"""``skills-hub collections`` / ``skills-hub collection show`` — E10.

Read-only из CLI: list + detail. CRUD (create/delete/edit) идёт через
Web UI; для CLI это редко нужно и тестируется отдельно при
необходимости.
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
    slug: str = typer.Argument(..., help="Slug коллекции"),
) -> None:
    """Detail + effective skills."""
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


def register(app: typer.Typer) -> None:
    """Register ``collections list`` + ``collection show``."""
    collections_app = typer.Typer(
        no_args_is_help=True, help="Skill collections (E10)"
    )
    collections_app.command("list")(cmd_collections_list)
    app.add_typer(collections_app, name="collections")

    collection_app = typer.Typer(
        no_args_is_help=True, help="Single collection"
    )
    collection_app.command("show")(cmd_collection_show)
    app.add_typer(collection_app, name="collection")
