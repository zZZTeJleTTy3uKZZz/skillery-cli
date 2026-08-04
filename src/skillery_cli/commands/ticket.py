"""``skillery ticket`` / ``skillery tickets`` — support tickets.

Команды:
- ``skillery ticket create <subject> [--skill slug] [--kind bug|feature|...]``
- ``skillery tickets list [--status open|...] [--kind ...]``
- ``skillery ticket show <id>``

Permission: ``ticket.create`` для create, ``ticket.read`` для list/show.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from clikit.command_kit import gated
from rich.console import Console
from rich.table import Table

from skillery_cli._grouping import deprecated_alias
from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data, emit_error

console = Console()

_KIND_CHOICES = ("bug", "feature", "question", "other")
_PRIORITY_CHOICES = ("low", "normal", "high", "urgent")
# NB: значения статусов для list-фильтра (legacy). Backend list-эндпоинт
# принимает их как фильтр-строки. Для записи статуса (PATCH) используется
# каноничный домен ``TicketStatus`` — см. ``_WRITE_STATUS_CHOICES`` ниже.
_STATUS_CHOICES = ("open", "in_progress", "resolved", "closed", "reopened")
# Реальные статусы из домена ``TicketStatus`` (backend) для ЗАПИСИ через
# ``PATCH /support/tickets/{id}``. Старые open/resolved/closed/reopened
# здесь НЕдопустимы — backend (``TicketStatus(body.status)``) вернул бы 422.
_WRITE_STATUS_CHOICES = ("new", "in_progress", "scheduled", "done", "rejected")


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
            # Канон (#1452): в ОТВЕТЕ размер страницы — только ``size``;
            # дубли ``page_size``/``has_more`` снесены. Запросный query-параметр
            # ``page_size`` бэкенд по-прежнему принимает (см. вызов выше).
            eff_size = p.get("size") or 1
            table = Table(
                title=(
                    f"Tickets (total={p.get('total')} page={p.get('page')}/"
                    f"{(p.get('total', 0) + eff_size - 1) // max(eff_size, 1)})"
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


def cmd_ticket_reply(
    ticket_id: str = typer.Argument(..., help="ID тикета"),
    body: str = typer.Argument(..., help="Текст сообщения в thread тикета"),
    parent_id: str | None = typer.Option(
        None, "--parent", help="ID родительского сообщения (для thread'а; только JSON)"
    ),
    screenshot: list[Path] = typer.Option(
        None,
        "--screenshot",
        help=(
            "Путь до файла скриншота. Можно повторять — пойдёт multipart "
            "(в multipart-режиме --parent backend'ом не принимается)."
        ),
    ),
) -> None:
    """Добавить сообщение (ответ) в thread тикета.

    Permission ``ticket.create`` (все участники треда). Если переданы
    скриншоты — ``POST /support/tickets/{id}/messages/multipart``, иначе
    JSON ``POST /support/tickets/{id}/messages``.
    """
    if not body.strip():
        emit_error("VALIDATION", "Текст сообщения не может быть пустым")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()
    files: list[tuple[str, bytes]] = []
    if screenshot:
        for p in screenshot:
            if not p.exists():
                emit_error("FILE_NOT_FOUND", f"Файл не найден: {p}")
                raise typer.Exit(1)
            files.append((p.name, p.read_bytes()))

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            if files:
                resp = await client.reply_ticket_multipart(
                    ticket_id, body=body, screenshots=files
                )
            else:
                resp = await client.reply_ticket(
                    ticket_id, body=body, parent_id=parent_id
                )
        finally:
            await client.close()
        message = resp["message"]
        payload: dict[str, Any] = {
            "event": "ticket_reply_posted",
            "ticket_id": ticket_id,
            "message": message,
        }

        def _render(p: dict[str, Any]) -> None:
            m = p["message"]
            console.print(
                f"[green]✓[/] Ответ {m['id']} → тикет {p['ticket_id']} "
                f"(screenshots={len(m.get('screenshots', []))})"
            )
            console.print(f"  {m['body']}")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_ticket_status(
    ticket_id: str = typer.Argument(..., help="ID тикета"),
    status: str = typer.Argument(
        ...,
        help=f"Новый статус: {', '.join(_WRITE_STATUS_CHOICES)}",
    ),
) -> None:
    """Сменить статус тикета (``PATCH /support/tickets/{id}``).

    Permission ``ticket.update_status`` (owner/manager). Допустимые статусы —
    из домена ``TicketStatus``: ``new | in_progress | scheduled | done |
    rejected``. Backend дополнительно проверяет валидность перехода (409/422
    при недопустимом).
    """
    if status not in _WRITE_STATUS_CHOICES:
        emit_error(
            "VALIDATION",
            f"status должен быть из {_WRITE_STATUS_CHOICES} (получено: {status})",
        )
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            t = await client.set_ticket_status(ticket_id, status=status)
        finally:
            await client.close()

        def _render(t: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Тикет {t['id']} → статус [bold]{t['status']}[/] "
                f"(kind={t['kind']} priority={t['priority']})"
            )
            console.print(f"  subject: {t['subject']}")

        emit_data(t, text_renderer=_render)

    _common.run(_do())


def register_ticket(
    app: typer.Typer, *, can_update: bool = False
) -> None:
    """Регистрирует sub-app ``ticket`` (create / show / reply [+ status]) +
    alias ``tickets`` (list).

    ``reply`` доступна всем участникам (gate ``ticket.create`` — там же где
    create). ``status`` — только при ``ticket.update_status`` (``can_update``);
    по умолчанию False (back-compat для существующих вызовов без аргумента).
    """
    ticket_app = typer.Typer(
        no_args_is_help=True,
        # ВНИМАНИЕ: rich_markup_mode="rich" — квадратные скобки в help
        # интерпретируются как разметка и роняют рендер справки.
        help="Тикеты поддержки: list / create / show / reply / status.",
    )
    ticket_app.command("create")(cmd_ticket_create)
    ticket_app.command("show")(cmd_ticket_show)
    ticket_app.command("reply")(cmd_ticket_reply)
    # #1223: список — глагол СВОЕГО ресурса (`ticket list`), а не отдельная
    # группа-множественное `tickets`.
    ticket_app.command("list")(cmd_tickets_list)
    # cli-kits W6: одиночный гейт status → command_kit.gated (предикат —
    # предвычисленный can_update).
    gated(ticket_app, permission="status", has_permission=lambda _p: can_update,
          name="status")(cmd_ticket_status)
    app.add_typer(ticket_app, name="ticket")

    # Back-compat: прежняя группа-множественное `tickets list` остаётся
    # рабочей СКРЫТОЙ формой и предупреждает о новом имени в stderr.
    tickets_app = typer.Typer(no_args_is_help=True, help="List support tickets")
    tickets_app.command("list", deprecated=True)(
        deprecated_alias(cmd_tickets_list, old="tickets list", new="ticket list")
    )
    app.add_typer(tickets_app, name="tickets", hidden=True)
