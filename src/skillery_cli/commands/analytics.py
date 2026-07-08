"""``skills-hub analytics local`` — локальная картина адопции (read-only).

Показывает БЕЗ сети и логина, что реально установлено на машине и каким
ИИ-агентом, плюс состояние очереди телеметрии:

- **store** — навыки, материализованные в центральном сторе (slug, версия,
  КАКОЙ агент ставил, source: hub/local-path/git-url);
- **project** — навыки, включённые в проект (project scope таргет-агента);
- **queue** — сколько событий в очереди + разбивка по типу и по агенту (что
  ещё не ушло на backend; daemon отправит batch'ем).

Команда строго read-only: ничего не пишет/не удаляет, сеть не трогает. Удобна
для дебага «почему дашборд не видит мою установку» и для AI-агентов (--json).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.config import ClientConfig
from skillery_cli.core import linker
from skillery_cli.core.agents import get_target
from skillery_cli.core.installer import read_meta
from skillery_cli.daemon.daemon_runner import default_queue_path
from skillery_cli.daemon.event_collector import EventCollector
from skillery_cli.output import emit_data

console = Console()


def _scan_store(store_root: Path) -> list[dict[str, Any]]:
    """Навыки в центральном сторе + meta (agent/source/version)."""
    items: list[dict[str, Any]] = []
    if not store_root.exists():
        return items
    for d in sorted(store_root.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        meta = read_meta(d) or {}
        items.append(
            {
                "name": d.name,
                "slug": meta.get("slug"),
                "skill_id": meta.get("skill_id"),
                "version": meta.get("version"),
                "agent": meta.get("agent"),
                "source": meta.get("source"),
                "scope": meta.get("scope"),
                "path": str(d),
            }
        )
    return items


def _scan_project(target, project: Path) -> list[dict[str, Any]]:  # noqa: ANN001
    """Навыки, включённые в project scope таргет-агента (наши, с meta)."""
    base = target.base_dir(project=project)
    items: list[dict[str, Any]] = []
    if not base.exists():
        return items
    for d in sorted(base.iterdir(), key=lambda p: p.name):
        meta = read_meta(d)
        if not meta:
            continue
        ref = meta.get("slug") or meta.get("skill_id") or d.name
        linked = linker.is_link(d)
        items.append(
            {
                "ref": ref,
                "slug": meta.get("slug"),
                "skill_id": meta.get("skill_id"),
                "version": meta.get("version"),
                "agent": meta.get("agent"),
                "source": meta.get("source"),
                "linked": linked,
                "path": str(d),
            }
        )
    return items


def _queue_stats(queue_path: Path) -> dict[str, Any]:
    """Размер очереди + разбивка по типу события и по агенту (read-only)."""
    coll = EventCollector(queue_path)
    events = coll.peek()
    by_type: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for e in events:
        by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
        agent = ""
        if isinstance(e.payload, dict):
            agent = str(e.payload.get("agent") or "")
        key = agent or "(unknown)"
        by_agent[key] = by_agent.get(key, 0) + 1
    return {
        "path": str(queue_path),
        "size": len(events),
        "by_type": by_type,
        "by_agent": by_agent,
    }


def cmd_analytics_local(
    project: Path | None = typer.Option(
        None, "--project", help="Корень проекта (default: config/cwd)"
    ),
) -> None:
    """Локальная аналитика адопции: стор + проект + очередь событий (read-only)."""
    cfg = ClientConfig.load()
    target = get_target(cfg.agent)
    actual_project = (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    )
    store_items = _scan_store(cfg.effective_store_dir())
    project_items = _scan_project(target, actual_project)
    queue = _queue_stats(default_queue_path())

    payload: dict[str, Any] = {
        "agent": target.name,
        "store_dir": str(cfg.effective_store_dir()),
        "store": store_items,
        "project": {
            "root": str(actual_project),
            "skills": project_items,
        },
        "queue": queue,
    }

    def _render(p: dict[str, Any]) -> None:
        console.print(f"Agent:       [bold]{p['agent']}[/]")
        console.print(f"Store dir:   {p['store_dir']}")
        # стор
        store = p["store"]
        if store:
            t = Table(title=f"Стор ({len(store)} навыков)")
            t.add_column("навык")
            t.add_column("version")
            t.add_column("агент")
            t.add_column("source")
            for s in store:
                t.add_row(
                    s.get("slug") or s["name"],
                    s.get("version") or "—",
                    s.get("agent") or "—",
                    s.get("source") or "—",
                )
            console.print(t)
        else:
            console.print("[dim]Стор пуст[/]")
        # проект
        proj = p["project"]
        psk = proj["skills"]
        console.print(f"Проект:      {proj['root']}")
        if psk:
            t = Table(title=f"Включено в проект ({len(psk)})")
            t.add_column("навык")
            t.add_column("version")
            t.add_column("агент")
            t.add_column("mount")
            for s in psk:
                t.add_row(
                    s.get("slug") or s.get("ref") or "—",
                    s.get("version") or "—",
                    s.get("agent") or "—",
                    "📎 link" if s.get("linked") else "📄 copy",
                )
            console.print(t)
        else:
            console.print("[dim]В проект ничего не включено[/]")
        # очередь
        q = p["queue"]
        console.print(
            f"Очередь:     {q['size']} событ{'ие' if q['size'] == 1 else 'ий'} "
            f"в {q['path']}"
        )
        if q["by_type"]:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(q["by_type"].items()))
            console.print(f"  по типу:   {parts}")
        if q["by_agent"]:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(q["by_agent"].items()))
            console.print(f"  по агенту: {parts}")

    emit_data(payload, text_renderer=_render)


def register(app: typer.Typer) -> None:
    """Register ``analytics local`` (always-on, read-only)."""
    an_app = typer.Typer(
        no_args_is_help=True, help="Локальная аналитика адопции"
    )
    an_app.command("local")(cmd_analytics_local)
    app.add_typer(an_app, name="analytics")
