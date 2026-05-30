"""``skills-hub contributors <id-или-slug>`` — список контрибьюторов скилла (E7).

GET /skills/{id}/contributors. Refresh-on-stale: если cache пустой —
backend сам резолвит из git.
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


def cmd_contributors(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Принудительно перечитать git"
    ),
) -> None:
    """Список контрибьюторов скилла."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = await _common.resolve_skill_id(client, slug)
            resp = await client.list_contributors(skill_id, refresh=refresh)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            rows = p.get("data") or []
            table = Table(title=f"Contributors ({skill_id})")
            table.add_column("email")
            table.add_column("name")
            table.add_column("commits")
            table.add_column("last_commit")
            for c in rows:
                table.add_row(
                    c.get("email") or "",
                    c.get("display_name") or "",
                    str(c.get("commit_count") or 0),
                    str(c.get("last_commit_at") or ""),
                )
            if not rows:
                console.print("[yellow]Контрибьюторов не найдено[/]")
            else:
                console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer) -> None:
    app.command(name="contributors")(cmd_contributors)
