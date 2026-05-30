"""``skills-hub ticket`` / ``skills-hub tickets`` — E8 support tickets.

Команды:
- ``skills-hub ticket create <subject> [--skill slug] [--kind bug|feature|...]``
- ``skills-hub tickets list [--status open|...] [--kind ...]``
- ``skills-hub ticket show <id>``

Permission: ``ticket.create`` для create, ``ticket.read`` для list/show.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.output import emit_data, emit_error

console = Console()

_KIND_CHOICES = ("bug", "feature", "question", "other")
_PRIORITY_CHOICES = ("low", "normal", "high", "urgent")
_STATUS_CHOICES = ("open", "in_progress", "resolved", "closed", "reopened")


def cmd_ticket_create(
    subject: str = typer.Argument(..., help="Тема тикета"),
    body: str = typer.Option(
        "",
        "--body",
        help="Описание (если пусто — берёт subject как body)",
    ),
    skill: str | None = typer.Option(
        None,
        "--skill",
        help="id-или-slug скилла (если тикет про конкретный skill)",
    ),
    kind: str = typer.Option(
        "other", "--kind", help=f"Тип: {', '.join(_KIND_CHOICES)}"
    ),
    priority: str = typer.Option(
        "normal",
        "--priority",
        help=f"Приоритет: {', '.join(_PRIORITY_CHOICES)}",
    ),
) -> None:
    """Создать support ticket."""
    if kind not in _KIND_CHOICES:
        emit_error("VALIDATION", f"kind должен быть из {_KIND_CHOICES}")
        raise typer.Exit(1)
    if priority not in _PRIORITY_CHOICES:
        emit_error("VALIDATION", f"priority должен быть из {_PRIORITY_CHOICES}")
        raise typer.Exit(1)
    actual_body = body or subject
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id: str | None = None
            if skill:
                skill_id = await _common.resolve_skill_id(client, skill)
            ticket = await client.create_ticket(
                subject=subject,
                body=actual_body,
                kind=kind,
                priority=priority,
                skill_id=skill_id,
            )
        finally:
            await client.close()

        def _render(t: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Ticket {t['id']} создан "
                f"(kind={t['kind']} priority={t['priority']} status={t['status']})"
            )
            console.print(f"  subject: {t['subject']}")
            if t.get("assignee_id"):
                console.print(f"  assignee: {t['assignee_id']}")

        emit_data(ticket, text_renderer=_render)

    _common.run(_do())


def cmd_tickets_list(
    status_: str | None = typer.Option(
        None, "--status", help=f"Filter: {', '.join(_STATUS_CHOICES)}"
    ),
    kind: str | None = typer.Option(None, "--kind"),
    priority: str | None = typer.Option(None, "--priority"),
    skill: str | None = typer.Option(None, "--skill", help="Filter by skill slug"),
    page: int = typer.Option(1, "--page", min=1),
    page_size: int = typer.Option(50, "--page-size", min=1, max=200),
) -> None:
    """Показать тикеты (scope-aware)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = None
            if skill:
                skill_id = await _common.resolve_skill_id(client, skill)
            resp = await client.list_tickets(
                status=status_,
                kind=kind,
                priority=priority,
                skill_id=skill_id,
                page=page,
                page_size=page_size,
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            items = p.get("items") or []
            table = Table(
                title=(
                    f"Tickets (total={p.get('total')} page={p.get('page')}/"
                    f"{(p.get('total', 0) + p.get('page_size', 1) - 1) // max(p.get('page_size', 1), 1)})"
                )
            )
            table.add_column("id")
            table.add_column("status")
            table.add_column("kind")
            table.add_column("priority")
            table.add_column("subject", overflow="fold")
            for t in items:
                table.add_row(
                    t["id"],
                    t["status"],
                    t["kind"],
                    t["priority"],
                    t["subject"],
                )
            if not items:
                console.print("[yellow]Тикетов не найдено[/]")
            else:
                console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_ticket_show(
    ticket_id: str = typer.Argument(..., help="ID тикета"),
) -> None:
    """Detail одного тикета."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            t = await client.get_ticket(ticket_id)
        finally:
            await client.close()

        def _render(t: dict[str, Any]) -> None:
            console.print(f"[bold]{t['subject']}[/] ({t['id']})")
            console.print(
                f"  status:    {t['status']}   "
                f"kind: {t['kind']}   priority: {t['priority']}"
            )
            console.print(f"  creator:   {t['creator_id']}")
            if t.get("assignee_id"):
                console.print(f"  assignee:  {t['assignee_id']}")
            if t.get("skill_id"):
                console.print(f"  skill:     {t['skill_id']}")
            console.print(f"  created:   {t['created_at']}")
            if t.get("resolved_at"):
                console.print(f"  resolved:  {t['resolved_at']}")
            console.print("")
            console.print(t.get("body") or "")

        emit_data(t, text_renderer=_render)

    _common.run(_do())


def register_ticket(app: typer.Typer) -> None:
    """Регистрирует sub-app ``ticket`` (create / show) + alias ``tickets`` (list)."""
    ticket_app = typer.Typer(no_args_is_help=True, help="Support tickets (E8)")
    ticket_app.command("create")(cmd_ticket_create)
    ticket_app.command("show")(cmd_ticket_show)
    app.add_typer(ticket_app, name="ticket")

    tickets_app = typer.Typer(no_args_is_help=True, help="List support tickets")
    tickets_app.command("list")(cmd_tickets_list)
    app.add_typer(tickets_app, name="tickets")
