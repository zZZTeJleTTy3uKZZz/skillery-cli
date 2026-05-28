"""``skills-hub comment`` + ``skills-hub comments`` (E7).

- ``skills-hub comment <slug> "body" [--screenshot path] [--parent id]``
  — добавить comment. Если есть screenshot — multipart upload.
- ``skills-hub comments <slug> [--limit N] [--cursor X]`` — list (public).

Permission: ``comment.post`` для post.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.output import emit_data, emit_error

console = Console()


def cmd_comment_post(
    slug: str = typer.Argument(..., help="Slug или id скилла"),
    body: str = typer.Argument(..., help="Текст комментария"),
    parent_id: str | None = typer.Option(
        None, "--parent", help="ID родительского comment'а для thread'а"
    ),
    screenshot: list[Path] = typer.Option(
        None,
        "--screenshot",
        help=(
            "Путь до файла скриншота. Можно повторять (до N раз) — "
            "пойдёт multipart upload."
        ),
    ),
) -> None:
    """Запостить comment (опционально со screenshots)."""
    if not body.strip():
        emit_error("VALIDATION", "Текст комментария не может быть пустым")
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
            skill_id = await _common.resolve_skill_id(client, slug)
            if files:
                resp = await client.post_comment_multipart(
                    skill_id,
                    body=body,
                    screenshots=files,
                    parent_id=parent_id,
                )
            else:
                resp = await client.post_comment(
                    skill_id, body=body, parent_id=parent_id
                )
        finally:
            await client.close()
        comment = resp["comment"]
        payload: dict[str, Any] = {
            "event": "comment_posted",
            "skill_id": skill_id,
            "comment": comment,
        }

        def _render(p: dict[str, Any]) -> None:
            c = p["comment"]
            console.print(
                f"[green]✓[/] Comment {c['id']} → {p['skill_id']} "
                f"(screenshots={len(c.get('screenshots', []))})"
            )
            console.print(f"  {c['body']}")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_comments_list(
    slug: str = typer.Argument(..., help="Slug или id скилла"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    starting_after: str | None = typer.Option(
        None, "--cursor", help="ID последнего comment'а с предыдущей страницы"
    ),
) -> None:
    """List комментариев (public + Stripe cursor pagination)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = await _common.resolve_skill_id(client, slug)
            page = await client.list_comments(
                skill_id, limit=limit, starting_after=starting_after
            )
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            rows = p.get("data") or []
            table = Table(title=f"Comments {skill_id} (has_more={p.get('has_more')})")
            table.add_column("id")
            table.add_column("user_id")
            table.add_column("body", overflow="fold")
            table.add_column("created")
            for c in rows:
                table.add_row(
                    c.get("id", ""),
                    (c.get("user_id") or "")[:12],
                    c.get("body") or "",
                    str(c.get("created_at") or ""),
                )
            if not rows:
                console.print("[yellow]Комментариев нет[/]")
            else:
                console.print(table)
            if p.get("next_cursor"):
                console.print(
                    f"[dim]→ следующая страница: --cursor {p['next_cursor']}[/]"
                )

        emit_data(page, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Регистрирует ``comment`` (post) + ``comments`` (list)."""
    app.command(name="comment")(cmd_comment_post)
    app.command(name="comments")(cmd_comments_list)
