"""``skillery comment`` + ``skillery comments``.

- ``skillery comment <id-или-slug> "body" [--screenshot path] [--parent id]``
  — добавить comment. Если есть screenshot — multipart upload.
- ``skillery comments <id-или-slug> [--page N] [--size N]`` — list (public).

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
    page_no: int = typer.Option(1, "--page", min=1),
    size: int = typer.Option(50, "--size", min=1, max=200),
) -> None:
    """List комментариев (public, offset-пагинация ``page``/``size``).

    REST-канон (#1452): Stripe-cursor (``--limit``/``--cursor``) снесён —
    у всех списков API одна форма страницы ``{items, total, page, size}``.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = await _common.resolve_skill_id(client, slug)
            page = await client.list_comments(skill_id, page=page_no, size=size)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            rows = p.get("items") or []
            table = Table(
                title=(
                    f"Comments {skill_id} "
                    f"(стр. {p.get('page', page_no)}, всего {p.get('total', len(rows))})"
                )
            )
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
            total = int(p.get("total") or 0)
            eff_size = int(p.get("size") or size or 1)
            cur = int(p.get("page") or page_no)
            if cur * eff_size < total:
                console.print(
                    f"[dim]→ следующая страница: --page {cur + 1}[/]"
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

# #2267: модульного ``register()`` здесь НЕТ и быть не должно. Он объявлял
# плоские ``comment``/``comments``, но не вызывался ниоткуда — реальная
# регистрация группы ``comment`` (add/edit/delete/list) живёт в ``build_app``,
# где доступны per-permission гейты (comment.post / edit_own / delete_own).
# Мёртвая вторая точка регистрации = второй источник правды о том, как
# называются команды; удалена.
