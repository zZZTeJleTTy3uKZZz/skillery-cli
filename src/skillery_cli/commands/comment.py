"""``skillery comment`` + ``skillery comments``.

- ``skillery comment <id-или-slug> "body" [--screenshot path] [--parent id]``
  — добавить comment. Если есть screenshot — multipart upload.
- ``skillery comments <id-или-slug> [--limit N] [--cursor X]`` — list (public).

Permission: ``comment.post`` для post.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data, emit_error

console = Console()


def cmd_comment_post(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
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
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
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


def cmd_comment_edit(
    comment_id: str = typer.Argument(..., help="Числовой id комментария"),
    body: str = typer.Argument(..., help="Новый текст комментария"),
) -> None:
    """Отредактировать свой комментарий (``PATCH /comments/{id}``).

    Permission ``comment.edit_own`` (только автор). Backend адресует comment по
    числовому id (резолв скилла не нужен). Возвращает обновлённый comment.
    """
    if not body.strip():
        emit_error("VALIDATION", "Текст комментария не может быть пустым")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            comment = await client.edit_comment(comment_id, body=body)
        finally:
            await client.close()

        def _render(c: dict[str, Any]) -> None:
            console.print(f"[green]✓[/] Comment {c['id']} отредактирован")
            console.print(f"  {c['body']}")

        emit_data(comment, text_renderer=_render)

    _common.run(_do())


def cmd_comment_delete(
    comment_id: str = typer.Argument(..., help="Числовой id комментария"),
) -> None:
    """Удалить (soft-delete) комментарий (``DELETE /comments/{id}``).

    Permission ``comment.delete_own`` (автор) ИЛИ
    ``comment.delete_any``/``hub.admin``/``skill.manage`` (модератор).
    Backend помечает ``is_deleted=True`` (body → ``[deleted]``).
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            comment = await client.delete_comment(comment_id)
        finally:
            await client.close()
        payload: dict[str, Any] = {
            "event": "comment_deleted",
            "comment": comment,
        }

        def _render(p: dict[str, Any]) -> None:
            c = p["comment"]
            console.print(
                f"[green]✓[/] Comment {c['id']} удалён "
                f"(is_deleted={c.get('is_deleted')})"
            )

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Регистрирует ``comment`` (post) + ``comments`` (list)."""
    app.command(name="comment")(cmd_comment_post)
    app.command(name="comments")(cmd_comments_list)
