"""Skill-side instrumentation: silent event push в очередь.

Когда CLI делает ``install`` / ``update`` / ``uninstall`` / ``run``, мы
кладём в очередь соответствующий event (``skill.install`` /
``skill.update`` / etc.). Daemon позже отправит batch.

Эта функция НЕ создаёт offending dependencies между __main__ и daemon:
если очередь не пишется (permission denied, missing dir) — всё
silently ignored. Telemetry никогда не ломает основную команду.
"""
from __future__ import annotations

from typing import Any

from skills_hub_cli.daemon.daemon_runner import default_queue_path
from skills_hub_cli.daemon.event_collector import EventCollector


def track_skill_event(
    event_type: str,
    *,
    slug: str,
    resource_id: str | None = None,
    version: str | None = None,
    scope: str | None = None,
    source: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Положить event в очередь. Никогда не raises (silent fail).

    Параметры:
        event_type: ``skill.install`` / ``skill.update`` / ``skill.enable``
            / ``skill.disable`` / ``skill.uninstall``.
            **Каноничное разделение** (план «Аналитика навыков» 2026-06-11):
            ``skill.install`` = материализация в стор, ``skill.enable`` =
            создана project-ссылка (включён в проект), ``skill.disable`` =
            ссылка снята (стор цел), ``skill.uninstall`` = удалён из стора.
        slug: slug скилла (идёт в payload как человекочитаемая метка).
        resource_id: идентификатор скилла-ресурса для аналитики — **строка**
            (``str(id)``), согласованная с backend/web (web ``track.ts`` шлёт
            ``resource_id: str(skill.id)``). PK миграция: id у сущностей —
            auto-increment int, на проводе сериализуется строкой; префиксов
            (``slk_``) больше нет. Если CLI не знает числовой id (типовой
            случай install/update/uninstall — на руках только slug), передаётся
            ``None`` и в качестве ссылки уходит ``slug``: backend-резолвер
            принимает id-или-slug, поэтому ссылка остаётся валидной.
        version: версия (если применимо).
        scope: ``global`` / ``project`` (КУДА линк — ось «scope»).
        source: ИСТОЧНИК навыка — ``hub`` / ``local-path`` / ``git-url``
            (резерв ``other-hub``). Отдельная ось аналитики «откуда ставят».
            В install-точках известен из контекста (hub-докачка → ``hub``,
            ``--path`` → ``local-path``, ``--from-git`` → ``git-url``); в
            re-link точках (enable/sync/collection/onboard) читается из
            ``read_meta(store/<slug>).get("source")`` — навык уже в сторе со
            своим source. ``None`` → ключ в payload не кладётся.
            NB: НЕ путать с ``metadata.source="cli"`` (транспортный канал —
            какой клиент прислал событие).
        extra: дополнительные поля в payload.
    """
    try:
        payload: dict[str, Any] = {"slug": slug}
        if version:
            payload["version"] = version
        if scope:
            payload["scope"] = scope
        if source:
            payload["source"] = source
        if extra:
            payload.update(extra)
        # resource_id — всегда строка (str(id) если известен, иначе slug).
        # Никаких prefixed-id (`slk_…`) — форма унифицирована с backend/web.
        ref = str(resource_id) if resource_id is not None else slug
        collector = EventCollector(default_queue_path())
        collector.append(
            event_type,
            resource_type="skill",
            resource_id=ref,
            payload=payload,
            metadata={"source": "cli"},
        )
    except Exception:
        # Telemetry никогда не ломает основную команду.
        pass
