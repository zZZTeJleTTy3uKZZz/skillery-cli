"""``skills-hub rate <slug> <score>`` — оценить скилл (E7).

Permission: ``skill.rate``. Backend upsert'ит rating (одна оценка на
user+skill пара).
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.output import emit_data, emit_error

console = Console()


def cmd_rate(
    slug: str = typer.Argument(..., help="Slug или id скилла"),
    score: int = typer.Argument(..., min=1, max=5, help="Оценка 1..5"),
) -> None:
    """Поставить оценку 1..5 (upsert).

    После успешной оценки печатает обновлённый summary (avg / count /
    distribution).
    """
    if score < 1 or score > 5:
        emit_error("VALIDATION", "Оценка должна быть в диапазоне 1..5")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = await _common.resolve_skill_id(client, slug)
            rating = await client.rate_skill(skill_id, score)
            summary = await client.get_rating_summary(skill_id)
        finally:
            await client.close()
        payload: dict[str, Any] = {
            "event": "rated",
            "skill_id": skill_id,
            "score": rating["score"],
            "summary": summary,
        }

        def _render(p: dict[str, Any]) -> None:
            s = p["summary"]
            console.print(
                f"[green]✓[/] Оценка {p['score']}★ сохранена ({p['skill_id']})"
            )
            console.print(
                f"  avg={s['avg']:.2f}  count={s['count']}  "
                f"distribution={s['distribution']}"
            )

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_rating_summary(
    slug: str = typer.Argument(..., help="Slug или id скилла"),
) -> None:
    """Показать только summary (avg/count/distribution) — без записи."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = await _common.resolve_skill_id(client, slug)
            summary = await client.get_rating_summary(skill_id)
        finally:
            await client.close()

        def _render(s: dict[str, Any]) -> None:
            console.print(f"[bold]Рейтинг {s['skill_id']}[/]")
            console.print(
                f"  avg={s['avg']:.2f}  count={s['count']}  "
                f"distribution={s['distribution']}"
            )

        emit_data(summary, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Регистрирует команды в главном app (зовётся из ``build_app``)."""
    app.command(name="rate")(cmd_rate)
    app.command(name="rating-summary")(cmd_rating_summary)
