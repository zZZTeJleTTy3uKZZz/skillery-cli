"""``skillery onboard`` — онбординг проекта.

Конвейер: детект сигналов проекта (``core/onboarding.detect_signals``) →
кандидаты из локального стора (+bounded hub-поиск ``GET /skills?q=…``, если
залогинен) → таблица предложений. ``--yes`` включает каждый не-``already``
кандидат в проект: есть в сторе → линк (как ``enable``), нет — докачка из
хаба через общий ``_install_chain`` (нужен login, иначе skip).

Без логина команда штатно работает только по локальному стору — это не
ошибка, hub-ветка просто не включается.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig, load_tokens
from skillery_cli.core import project_manifest
from skillery_cli.core.agents import get_target
from skillery_cli.core.installer import SkillInstaller, read_meta
from skillery_cli.core.onboarding import (
    detect_signals,
    match_store,
    merge_suggestions,
)
from skillery_cli.daemon.instrumentation import track_skill_event
from skillery_cli.output import emit_data, emit_error

console = Console()

# Канон bounded search (пикер-канон): hub-поиск не шире 20 элементов на сигнал.
_MAX_HUB_SIZE = 20

# Lazy-биндинг ``_install_chain`` из ``__main__`` (как commands/collection.py):
# импорт ``__main__`` на верхнем уровне триггерит build_app(). Тесты
# монкипатчат ``onboard._install_chain`` напрямую — placeholder это позволяет.
_install_chain = None  # type: ignore[assignment]


def _ensure_install_helpers() -> None:
    """Подтянуть ``_install_chain`` из ``__main__`` (не перетирая monkeypatch)."""
    global _install_chain
    if _install_chain is None:
        from skillery_cli import __main__ as _main

        _install_chain = _main._install_chain


async def _hub_candidates(
    cfg: ClientConfig, access: str, signals: list[str], limit: int
) -> list[dict[str, Any]]:
    """Bounded hub-поиск: ``GET /skills?q=<signal>&size=N`` на каждый сигнал.

    Дедуп по slug (slug-less навык — по числовому id); каждому кандидату
    копится список сигналов, по которым он найден. Ошибка поиска по одному
    сигналу не валит остальные (деградация на локальный стор).
    """
    size = max(1, min(limit, _MAX_HUB_SIZE))
    by_slug: dict[str, dict[str, Any]] = {}
    client = _common.make_client(cfg, access)
    try:
        for sig in signals:
            try:
                resp = await client.search_skills(q=sig, size=size)
            except Exception:  # noqa: BLE001 — hub недоступен ≠ провал onboard
                continue
            for item in (resp or {}).get("items") or []:
                slug = item.get("slug") or str(item.get("id") or "")
                if not slug:
                    continue
                entry = by_slug.setdefault(
                    slug, {"slug": slug, "title": item.get("title"), "signals": []}
                )
                if sig not in entry["signals"]:
                    entry["signals"].append(sig)
    finally:
        await client.close()
    return list(by_slug.values())


def _render_suggestions(p: dict[str, Any]) -> None:
    signals = p.get("signals") or []
    suggestions = p.get("suggestions") or []
    console.print(f"[bold]Онбординг проекта[/] {p.get('project')}")
    console.print(f"  Сигналы: {', '.join(signals) or '—'}")
    if not suggestions:
        console.print(
            "[yellow]Подходящих навыков не найдено[/] "
            "(нет сигналов или пустой стор; залогиньтесь для поиска по хабу)"
        )
        return
    table = Table(title=f"Предложения ({len(suggestions)})")
    table.add_column("slug")
    table.add_column("источник")
    table.add_column("сигналы")
    table.add_column("статус")
    for s in suggestions:
        table.add_row(
            s["slug"],
            s["source"],
            ", ".join(s.get("signals") or []),
            "уже включён" if s.get("already") else "",
        )
    console.print(table)
    console.print("[dim]Включить всё в проект: skillery onboard --yes[/]")


def _render_applied(p: dict[str, Any]) -> None:
    applied = p["applied"]
    console.print(f"[bold]Онбординг применён[/] ({p.get('project')})")
    for slug in applied["linked"]:
        console.print(f"  [green]✓[/] включён из стора 📎 {slug}")
    for slug in applied["installed"]:
        console.print(f"  [green]✓[/] докачан из хаба ⬇ {slug}")
    for slug in applied["already"]:
        console.print(f"  [dim]= уже включён: {slug}[/]")
    for item in applied["skipped"]:
        console.print(f"  [yellow]→ пропущен[/] {item['slug']}: {item['reason']}")
    if not any(applied[k] for k in ("linked", "installed", "already", "skipped")):
        console.print("[yellow]Нечего применять — предложений нет[/]")


def cmd_onboard(
    project: Path | None = typer.Option(
        None, "--project", help="Корень проекта (default: cwd)"
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Сразу включить предложенные навыки в проект"
    ),
    limit: int = typer.Option(
        10, "--limit", help="Сколько кандидатов тянуть с хаба на сигнал (капится 20)"
    ),
    agent: str | None = typer.Option(None, "--agent"),
) -> None:
    """Онбординг проекта: детект стека → подбор навыков (стор + хаб) → enable.

    Без ``--yes`` — только показ предложений (``--json`` отдаёт
    machine-readable ``{signals, suggestions}``). С ``--yes`` каждый
    не-``already`` кандидат включается в проект: стор → линк + манифест;
    нет в сторе — докачка из хаба (требует login, иначе skip). Без логина
    команда работает по локальному стору — это штатный режим.
    """
    cfg = ClientConfig.load()
    actual_project = (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    ).resolve()
    if not actual_project.is_dir():
        emit_error("VALIDATION", f"Папка проекта не найдена: {actual_project}")
        raise typer.Exit(1)

    signals = detect_signals(actual_project)
    local = match_store(cfg.effective_store_dir(), signals)

    access = ""
    if signals and cfg.is_logged_in():
        token, _ = load_tokens(cfg.user_email or "")
        access = token or ""

    async def _do() -> None:
        hub: list[dict[str, Any]] = []
        if access:
            hub = await _hub_candidates(cfg, access, signals, limit)
        installed = set(project_manifest.load(actual_project))
        suggestions = merge_suggestions(local, hub, installed)

        if not yes:
            emit_data(
                {
                    "project": str(actual_project),
                    "signals": signals,
                    "suggestions": suggestions,
                },
                text_renderer=_render_suggestions,
            )
            return

        # --- --yes: включаем каждый не-already кандидат (идемпотентно) ---
        target = get_target(agent or cfg.agent)
        installer = SkillInstaller(target, cfg.effective_store_dir())
        applied: dict[str, list[Any]] = {
            "linked": [], "installed": [], "already": [], "skipped": [],
        }
        for s in suggestions:
            slug = s["slug"]
            if s["already"]:
                applied["already"].append(slug)
                continue
            # Стор-first: материализован локально → линк без сети (как enable).
            local_link = installer.link_existing(slug, project=actual_project)
            if local_link is not None:
                project_manifest.add(actual_project, slug)
                store_meta = read_meta(cfg.effective_store_dir() / slug) or {}
                # Навык уже в сторе → re-link = включение-в-проект →
                # skill.enable (source из meta стора), НЕ повторный install.
                track_skill_event(
                    "skill.enable", slug=slug,
                    version=store_meta.get("version") or "", scope="project",
                    source=store_meta.get("source"), agent=target.name,
                )
                applied["linked"].append(slug)
                continue
            # Нет в сторе → докачка из хаба (только при наличии сессии).
            if not access:
                applied["skipped"].append({"slug": slug, "reason": "not_logged_in"})
                continue
            _ensure_install_helpers()
            try:
                await _install_chain(
                    cfg, access, slug=slug, channel="published", scope="project",
                    project_path=actual_project, force=False, agent_target=target,
                )
                project_manifest.add(actual_project, slug)
                applied["installed"].append(slug)
            except Exception as exc:  # noqa: BLE001 — копим ошибки, не падаем на первой
                applied["skipped"].append({"slug": slug, "reason": str(exc)})

        emit_data(
            {
                "project": str(actual_project),
                "signals": signals,
                "suggestions": suggestions,
                "applied": applied,
            },
            text_renderer=_render_applied,
        )

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Регистрирует always-on команду ``onboard``."""
    app.command(name="onboard")(cmd_onboard)
