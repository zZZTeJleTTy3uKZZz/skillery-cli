"""``skills-hub suggest "<query>"`` — discovery навыков по свободному запросу.

Always-on, read-only: ничего не ставит, только подсказывает что и как включить.

Конвейер:

1. :func:`core.suggest.normalize_query` — запрос → значимые ``terms``.
2. Локальные кандидаты: скан стора (``read_meta`` каждого навыка) →
   :func:`core.suggest.score_skill` (с ``local_boost`` — локальное выше при
   равном score) → ``source=local``; ``status`` (in_store / installed_global /
   installed_project) из скана scope-папок агента; ``already_enabled`` из
   проектного манифеста ``.skills-hub/skills.toml``.
3. Если залогинен — bounded hub-поиск: ``GET /skills?q=<term>&size≤20`` на
   каждый term (RBAC-видимость несёт backend), дедуп по slug. Ошибка
   токена/сети деградирует на локальную выдачу (команда не валится).
4. ``merge_suggestions`` (DRY — общий с онбордингом) сводит local+hub в
   ``source local|hub|both``; сортировка по ``score`` desc (local выше hub
   при равенстве).

Каждый suggestion несёт ``install_cmd`` — готовую строку ``skills-hub enable
<slug> --project <p>`` (исполнение оставляем пользователю).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig, load_tokens
from skills_hub_cli.core import project_manifest
from skills_hub_cli.core.agents import get_target
from skills_hub_cli.core.installer import read_meta
from skills_hub_cli.core.onboarding import merge_suggestions
from skills_hub_cli.core.suggest import normalize_query, score_skill
from skills_hub_cli.output import emit_data, emit_error

console = Console()

# Канон bounded search (пикер-канон): hub-поиск не шире 20 элементов на term.
_MAX_HUB_SIZE = 20

# Лёгкий приоритет локальному навыку при равном score: при сортировке local/both
# поднимается над hub-only кандидатом с тем же весом (он уже у пользователя в
# сторе — включить дешевле, чем докачивать).
_LOCAL_BOOST = 0.5


def _local_candidates(
    store_dir: Path, terms: list[str]
) -> list[dict[str, Any]]:
    """Скан стора → заскоренные локальные кандидаты (source=local).

    Каждому навыку считаем ``score_skill`` против ``terms`` (с ``local_boost``);
    нулевой матч пропускаем. Несём ``score``/``matched_on``/``title``/
    ``version`` — статус и already_enabled добавляются позже.
    """
    root = Path(store_dir)
    out: list[dict[str, Any]] = []
    if not terms or not root.is_dir():
        return out
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        meta = read_meta(d)
        if meta is None:
            continue
        score, matched_on = score_skill(meta, terms, local_boost=_LOCAL_BOOST)
        if score <= 0:
            continue
        slug = str(meta.get("slug") or d.name)
        out.append(
            {
                "slug": slug,
                "title": meta.get("title") or slug,
                "version": meta.get("version"),
                "score": score,
                "matched_on": matched_on,
                # core.merge ждёт ключ signals (терм-объяснимость) — даём termы.
                "signals": list(terms),
            }
        )
    return out


async def _hub_candidates(
    cfg: ClientConfig, access: str, terms: list[str], limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Bounded hub-поиск: ``GET /skills?q=<term>&size≤20`` на каждый term.

    Дедуп по slug; каждому кандидату считаем ``score_skill`` по его полям
    (объяснимость и сортировка едины с локальными). Возвращает
    ``(candidates, degraded)``: ``degraded=True`` если хотя бы один term-поиск
    упал (RBAC/токен/сеть) — команда не валится, лишь добавит note.
    """
    size = max(1, min(limit, _MAX_HUB_SIZE))
    by_slug: dict[str, dict[str, Any]] = {}
    degraded = False
    client = _common.make_client(cfg, access)
    try:
        for term in terms:
            try:
                resp = await client.search_skills(q=term, size=size)
            except Exception:  # noqa: BLE001 — хаб недоступен ≠ провал команды
                degraded = True
                continue
            for item in (resp or {}).get("items") or []:
                slug = item.get("slug") or str(item.get("id") or "")
                if not slug or slug in by_slug:
                    continue
                # RBAC несёт backend (visibility) — скорим только для ранга.
                score, matched_on = score_skill(item, terms)
                by_slug[slug] = {
                    "slug": slug,
                    "title": item.get("title") or slug,
                    "version": _latest_version(item),
                    "score": score,
                    "matched_on": matched_on,
                    "signals": list(terms),
                }
    finally:
        await client.close()
    return list(by_slug.values()), degraded


async def _hub_candidates_semantic(
    cfg: ClientConfig, access: str, query: str, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """#242 AI-ветка: один POST /skills/search-semantic вместо per-term поиска.

    Возвращает ``(candidates, degraded)``. Каждый кандидат несёт ``score`` и
    ``reason`` ОТ БЭКА (семантическая объяснимость), а также ``matched_on=[]``
    (лексических совпадений тут нет — reason заменяет). ``degraded=True`` при
    ошибке (токен/сеть/нет эндпоинта) — команда деградирует на локальную выдачу,
    не валится.
    """
    top_k = max(1, min(limit, _MAX_HUB_SIZE))
    client = _common.make_client(cfg, access)
    out: list[dict[str, Any]] = []
    try:
        try:
            resp = await client.search_skills_semantic(query=query, top_k=top_k)
        except Exception:  # noqa: BLE001 — семантика недоступна ≠ провал команды
            return [], True
        for m in (resp or {}).get("matches") or []:
            slug = m.get("slug") or str(m.get("skill_id") or "")
            if not slug:
                continue
            out.append(
                {
                    "slug": slug,
                    "title": m.get("title") or slug,
                    "version": None,
                    "score": float(m.get("score") or 0.0),
                    "matched_on": [],
                    "reason": m.get("reason") or "",
                    "signals": normalize_query(query),
                }
            )
    finally:
        await client.close()
    return out, False


def _latest_version(item: dict[str, Any]) -> str | None:
    """Лучшая версия из hub-DTO (``versions[].semver``), иначе ``version``."""
    versions = item.get("versions") or []
    if versions and isinstance(versions[0], dict):
        return versions[0].get("semver")
    return item.get("version")


def _skill_status(
    target, slug: str, *, project: Path  # type: ignore[no-untyped-def]
) -> str:
    """Где навык: installed_project > installed_global > in_store > not_installed.

    Локальные кандидаты заведомо ≥ ``in_store`` (пришли из скана стора); hub-
    only — ``not_installed`` пока не материализован в один из scope.
    """
    if target.slug_dir(slug, project=project).exists():
        return "installed_project"
    if target.slug_dir(slug, project=None).exists():
        return "installed_global"
    return "in_store"


def _render(p: dict[str, Any]) -> None:
    suggestions = p.get("suggestions") or []
    console.print(f"[bold]Подбор навыков[/] по запросу: «{p.get('query')}»")
    console.print(f"  Termы: {', '.join(p.get('terms') or []) or '—'}")
    if not suggestions:
        console.print("[yellow]Подходящих навыков не найдено[/]")
    else:
        # #242: колонка «причина» показывается, только если хоть один кандидат
        # её несёт (семантический режим --ai) — лексическая выдача её не имеет.
        show_reason = any(s.get("reason") for s in suggestions)
        table = Table(title=f"Предложения ({len(suggestions)})")
        table.add_column("slug")
        table.add_column("источник")
        table.add_column("score", justify="right")
        table.add_column("статус")
        table.add_column("в проекте")
        if show_reason:
            table.add_column("причина")
        for s in suggestions:
            row = [
                s["slug"],
                s["source"],
                f"{s['score']:.1f}",
                s["status"],
                "✓" if s["already_enabled"] else "",
            ]
            if show_reason:
                row.append(s.get("reason") or "")
            table.add_row(*row)
        console.print(table)
        console.print(
            "[dim]Включить: команда в поле install_cmd "
            "(skillery enable <slug> --project <путь>)[/]"
        )
    for note in p.get("notes") or []:
        console.print(f"[dim]• {note}[/]")


def cmd_suggest(
    query: str = typer.Argument(
        ..., metavar="ЗАПРОС", help="Свободный запрос: «нужен stripe для платежей»"
    ),
    project: Path | None = typer.Option(
        None, "--project", help="Корень проекта (default: cwd) — для install_cmd"
    ),
    limit: int = typer.Option(
        10, "--limit", help="Сколько кандидатов тянуть с хаба на term (капится 20)"
    ),
    ai: bool = typer.Option(
        False,
        "--ai",
        help=(
            "#242: семантический подбор по хабу (POST /skills/search-semantic) "
            "вместо лексического. Требует логина; локальный стор остаётся на "
            "лексике (оффлайн-фолбэк). При ошибке/без логина — деградирует."
        ),
    ),
    agent: str | None = typer.Option(None, "--agent"),
) -> None:
    """Подобрать навыки под свободный запрос (стор + хаб). Read-only.

    Ничего не ставит — только показывает кандидатов и готовую команду включения
    (``install_cmd``). Без логина работает по локальному стору (это штатно);
    hub-поиск включается при наличии сессии и деградирует при ошибке токена.
    """
    # При прямом вызове (тесты) необъявленный флаг приходит как typer.OptionInfo
    # (truthy) — коэрсим к строгому bool, чтобы --ai включался ТОЛЬКО явно.
    ai = ai is True
    cfg = ClientConfig.load()
    actual_project = (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    ).resolve()
    target = get_target(agent or cfg.agent)

    terms = normalize_query(query)
    notes: list[str] = []

    local = _local_candidates(cfg.effective_store_dir(), terms)

    access = ""
    logged_in = cfg.is_logged_in()
    # #242: в AI-режиме семантика гоняется по сырому query (а не terms) — токен
    # нужен, даже если лексическая нормализация дала пусто.
    if logged_in and (terms or ai):
        token, _ = load_tokens(cfg.user_email or "")
        access = token or ""

    if not terms and not ai:
        notes.append(
            "Запрос не дал значимых слов — уточните (например: «stripe платежи»)."
        )

    async def _do() -> None:
        nonlocal notes
        hub: list[dict[str, Any]] = []
        if ai and access:
            # #242 AI-ветка: семантический подбор по хабу (один POST). Локальный
            # стор остаётся лексическим (оффлайн-фолбэк) — он уже посчитан выше.
            hub, degraded = await _hub_candidates_semantic(
                cfg, access, query, limit
            )
            if degraded:
                notes.append(
                    "Семантический поиск по хабу не удался (токен/сеть/не "
                    "поддержан) — показаны локальные/лексические кандидаты."
                )
        elif ai and not logged_in:
            notes.append(
                "--ai требует логина (семантика считается на хабе). "
                "Залогиньтесь (skillery login); пока — локальный стор."
            )
        elif terms and access:
            hub, degraded = await _hub_candidates(cfg, access, terms, limit)
            if degraded:
                notes.append(
                    "Поиск по хабу частично не удался (токен/сеть) — "
                    "показаны локальные кандидаты."
                )
        elif terms and not logged_in:
            notes.append(
                "Не залогинены — ищем только в локальном сторе. "
                "Залогиньтесь (skillery login) для поиска по хабу."
            )

        installed = set(project_manifest.load(actual_project))
        merged = merge_suggestions(local, hub, installed)

        # merge теряет score/matched_on/title/version — добираем из источников
        # (local приоритетнее: он несёт local_boost и материализованную версию).
        # #242: ``reason`` (семантическая объяснимость) живёт ТОЛЬКО на hub-
        # кандидатах — фиксируем отдельной картой, чтобы local-перетирание не
        # стёрло его у both-источников.
        reason_by_slug: dict[str, str] = {
            item["slug"]: item.get("reason") or ""
            for item in hub
            if item.get("reason")
        }
        meta_by_slug: dict[str, dict[str, Any]] = {}
        for item in hub:
            meta_by_slug[item["slug"]] = item
        for item in local:  # local перетирает hub — приоритет локальным данным
            meta_by_slug[item["slug"]] = item

        local_slugs = {item["slug"] for item in local}
        suggestions: list[dict[str, Any]] = []
        for entry in merged:
            slug = entry["slug"]
            src = meta_by_slug.get(slug, {})
            is_local = slug in local_slugs
            status = (
                _skill_status(target, slug, project=actual_project)
                if is_local
                else "not_installed"
            )
            suggestions.append(
                {
                    "slug": slug,
                    "title": src.get("title") or slug,
                    "source": entry["source"],
                    "score": float(src.get("score") or 0.0),
                    "matched_on": src.get("matched_on") or [],
                    # #242: обратносовместимое расширение контракта — reason
                    # опционален (пусто для чисто-лексической выдачи).
                    "reason": reason_by_slug.get(slug, src.get("reason") or ""),
                    "status": status,
                    "already_enabled": bool(entry.get("already")),
                    "version": src.get("version"),
                    "install_cmd": (
                        f"skillery enable {slug} --project {actual_project}"
                    ),
                }
            )

        # Сортировка: score desc; при равенстве local/both выше hub-only.
        def _sort_key(s: dict[str, Any]) -> tuple[float, int]:
            local_rank = 1 if s["source"] in ("local", "both") else 0
            return (s["score"], local_rank)

        suggestions.sort(key=_sort_key, reverse=True)

        if terms and not suggestions:
            notes.append("Подходящих навыков не нашлось — попробуйте другие слова.")

        emit_data(
            {
                "query": query,
                "logged_in": logged_in,
                "project": str(actual_project),
                "terms": terms,
                "suggestions": suggestions,
                "notes": notes,
            },
            text_renderer=_render,
        )

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Регистрирует always-on read-only команду ``suggest``."""
    app.command(name="suggest")(cmd_suggest)
