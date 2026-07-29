"""``skillery event track / queue / flush`` — аналитика в ОБЩЕЙ очереди.

Команда НЕ отправляет event напрямую в backend: пишет конверт
``kind="analytics_event"`` в общий outbox (``~/.skillery/outbox.jsonl``, см.
:mod:`skillery_cli.core.analytics_sync`). Доставку делает единый воркер
(:mod:`skillery_cli.core.outbox_worker`) в цикле демона — тем же проходом, что
везёт запуски навыков и логи CLI.

#1180: своей очереди (``events.queue.json``) у аналитики больше нет. Очередь на
машине ОДНА; ``event queue --clear`` поэтому трогает ТОЛЬКО аналитические
конверты — выбрасывать чужие (логи, запуски навыков) он права не имеет.

Если демон не запущен — события накапливаются; первый же его старт их подтянет,
а ``event flush`` шлёт синхронно и прямо сейчас (в т.ч. АНОНИМНО, без логина).
"""
from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.core import analytics_sync
from skillery_cli.daemon.event_sender import OutboxSender
from skillery_cli.output import emit_data, emit_error

console = Console()


def _parse_json_dict(raw: str | None, *, field: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        emit_error("VALIDATION", f"--{field}: невалидный JSON: {e}")
        raise typer.Exit(1) from e
    if not isinstance(data, dict):
        emit_error("VALIDATION", f"--{field}: ожидается JSON-объект")
        raise typer.Exit(1)
    return data


def cmd_event_track(
    event_type: str = typer.Argument(..., help="Тип события, напр. skill.run"),
    resource_type: str | None = typer.Option(
        None, "--resource-type", help="Тип ресурса (skill / collection / ...)"
    ),
    resource_id: str | None = typer.Option(None, "--resource-id"),
    payload: str | None = typer.Option(
        None, "--payload", help="JSON-объект с полезной нагрузкой"
    ),
    metadata: str | None = typer.Option(
        None, "--metadata", help="JSON-объект с метаданными"
    ),
) -> None:
    """Положить event в общую исходящую очередь (демон отправит батчем)."""
    if len(event_type) < 3 or len(event_type) > 64:
        emit_error("VALIDATION", "event_type должен быть 3..64 chars")
        raise typer.Exit(1)
    pl = _parse_json_dict(payload, field="payload")
    md = _parse_json_dict(metadata, field="metadata")
    item = analytics_sync.track(
        event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        payload=pl,
        metadata=md,
    )
    if item is None:
        emit_error(
            "QUEUE_UNAVAILABLE",
            "очередь недоступна (outbox выключен или каталог не пишется)",
        )
        raise typer.Exit(1)
    out: dict[str, Any] = {
        "event": "queued",
        "queued_event": item,
        "queue_size": analytics_sync.pending_count(),
    }

    def _render(p: dict[str, Any]) -> None:
        console.print(
            f"[green]✓[/] Event '{event_type}' в очереди "
            f"(size={p['queue_size']})"
        )

    emit_data(out, text_renderer=_render)


def cmd_event_queue(
    show: bool = typer.Option(
        False, "--show", help="Распечатать содержимое очереди"
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Очистить очередь (опасно!)"
    ),
) -> None:
    """Inspect / clear аналитические события в общей исходящей очереди."""
    if clear:
        removed = analytics_sync.clear_pending()
        emit_data(
            {"event": "queue_cleared", "removed": removed},
            text_renderer=lambda p: console.print(
                f"[yellow]Очистил очередь: удалено {p['removed']} events[/]"
            ),
        )
        return
    events = analytics_sync.pending()
    queue_path = analytics_sync.outbox_path()
    payload = {
        "queue_path": str(queue_path) if queue_path else "—",
        "size": len(events),
        "events": events if show else [],
    }

    def _render(p: dict[str, Any]) -> None:
        console.print(f"Queue: {p['queue_path']}  size={p['size']}")
        if show and p["events"]:
            table = Table(title="Pending events")
            table.add_column("type")
            table.add_column("occurred_at")
            table.add_column("resource")
            for e in p["events"]:
                res = (
                    f"{e.get('resource_type')}/{e.get('resource_id')}"
                    if e.get("resource_type")
                    else ""
                )
                table.add_row(e["event_type"], e["occurred_at"], res)
            console.print(table)

    emit_data(payload, text_renderer=_render)


def cmd_event_flush() -> None:
    """Синхронно отправить накопленное (один проход воркера, минуя троттл).

    ``POST /events`` анонимен (backend ``_optional_claims``): отсутствие или
    протухание токена НЕ должно ронять flush — аналитика уходит как anonymous.
    Поэтому токен берётся best-effort (без exit(1) при NOT_LOGGED_IN/NO_TOKEN),
    а фабрика умеет строить anonymous-клиент (``anonymous=True`` → без Bearer).
    """
    from skillery_cli.config import load_tokens

    cfg = ClientConfig.load()
    # best-effort: нет email/токена → пустая строка → client без Bearer.
    access = ""
    if cfg.user_email:
        access, _ = load_tokens(cfg.user_email)
        access = access or ""

    def _factory(anonymous: bool = False):  # type: ignore[no-untyped-def]
        if anonymous:
            # Деградировать некуда, если токена и так не было.
            if not access:
                return None
            return _common.make_client(cfg, "")
        return _common.make_client(cfg, access)

    sender = OutboxSender(_factory)

    async def _do() -> None:
        result = await sender.send_once(force=True)
        payload = {
            "event": "flushed",
            "sent": result.sent,
            "accepted": result.accepted,
            "skipped": result.skipped,
            "requeued": result.requeued,
            "last_error": result.last_error,
        }

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Flushed: sent={p['sent']} "
                f"accepted={p['accepted']} requeued={p['requeued']}"
            )
            if p["last_error"]:
                console.print(f"[yellow]Last error: {p['last_error']}[/]")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_event_list(
    event_type: str | None = typer.Option(None, "--type", help="Тип события"),
    actor_id: str | None = typer.Option(None, "--actor", help="ID актора"),
    company_id: str | None = typer.Option(None, "--company", help="ID компании"),
    resource_type: str | None = typer.Option(
        None, "--resource-type", help="Тип ресурса"
    ),
    resource_id: str | None = typer.Option(None, "--resource-id", help="ID ресурса"),
    since: str | None = typer.Option(None, "--since", help="С момента (ISO 8601)"),
    until: str | None = typer.Option(None, "--until", help="До момента (ISO 8601)"),
    page: int = typer.Option(1, "--page", help="Страница (с 1)"),
    size: int = typer.Option(50, "--size", help="Размер страницы (макс. 200)"),
) -> None:
    """Лента аналитических событий хаба (GET /events) — #1224.

    Это ЧТЕНИЕ с сервера, в отличие от ``event track``/``flush``, которые
    работают с локальной очередью отправки. Право — ``hub.admin``.

    CLI всегда ходит в offset-режиме (``page``+``size``): в legacy-курсорном
    у backend ``total`` считает строки текущей страницы, а не всё совпадение,
    и по нему нельзя оценить объём выборки.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_events(
                event_type=event_type,
                actor_id=actor_id,
                company_id=company_id,
                resource_type=resource_type,
                resource_id=resource_id,
                since=since,
                until=until,
                page=page,
                size=size,
            )
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            items = payload.get("items") or []
            if not items:
                console.print("[yellow]Событий не найдено[/]")
                return
            table = Table(
                title=f"События (всего: {payload.get('total', len(items))})"
            )
            table.add_column("когда")
            table.add_column("тип", overflow="fold")
            table.add_column("актор", overflow="fold")
            table.add_column("ресурс", overflow="fold")
            for item in items:
                actor = item.get("actor") or {}
                resource = item.get("resource") or {}
                table.add_row(
                    str(item.get("occurred_at") or "—"),
                    str(item.get("event_type") or "—"),
                    str(
                        actor.get("display_name")
                        or actor.get("email")
                        or actor.get("type")
                        or "—"
                    ),
                    (
                        f"{resource.get('type')}:{resource.get('id')}"
                        if resource
                        else "—"
                    ),
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer, *, can_read_hub: bool = False) -> None:
    """Register ``event track / queue / flush`` (+ ``list`` при hub.admin)."""
    event_app = typer.Typer(no_args_is_help=True, help="Event tracking")
    event_app.command("track")(cmd_event_track)
    event_app.command("queue")(cmd_event_queue)
    event_app.command("flush")(cmd_event_flush)
    if can_read_hub:
        # GET /events — hub.admin-only; без права команда только сбивала бы с
        # толку гарантированным 403.
        event_app.command("list")(cmd_event_list)
    app.add_typer(event_app, name="event")
