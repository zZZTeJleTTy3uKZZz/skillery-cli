"""Чистые функции онбординга проекта — без CLI и без сети.

Три шага конвейера ``skills-hub onboard``:

1. :func:`detect_signals` — V1-эвристика «какие технологии живут в проекте»
   по маркер-файлам корня (pyproject/package.json/Dockerfile/...).
2. :func:`match_store` — кандидаты из локального стора: пересечение
   ``manifest.tags``/``description``/slug каждого ``_skill_meta.json``
   с сигналами.
3. :func:`merge_suggestions` — объединение local+hub кандидатов: дедуп по
   slug, источник ``local|hub|both``, пометка ``already`` для уже включённых
   в проект (``.skillery/skills.toml``).

Все функции тестируются на голых Path-фикстурах (``test_p1_onboard.py``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skillery_cli.core.installer import read_meta


def detect_signals(project: Path) -> list[str]:
    """Детект сигналов проекта (V1-эвристика по маркер-файлам корня).

    - ``pyproject.toml`` | ``requirements.txt`` → ``python``;
    - ``package.json`` → ``nodejs`` (+``next``→``nextjs``, +``react``→``react``
      по ключам dependencies/devDependencies; битый JSON не ломает детект);
    - ``Dockerfile`` | ``docker-compose*`` → ``docker``;
    - ``*.tf`` → ``terraform``;
    - ``go.mod`` → ``go``;
    - ``Cargo.toml`` → ``rust``;
    - ``.claude/`` → ``claude-code``.

    Возвращает список без дубликатов в стабильном порядке обнаружения.
    """
    root = Path(project)
    signals: list[str] = []

    def _add(sig: str) -> None:
        if sig not in signals:
            signals.append(sig)

    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        _add("python")

    pkg = root / "package.json"
    if pkg.is_file():
        _add("nodejs")
        deps: dict[str, Any] = {}
        try:
            data = json.loads(pkg.read_text(encoding="utf-8-sig"))
            for key in ("dependencies", "devDependencies"):
                section = data.get(key)
                if isinstance(section, dict):
                    deps.update(section)
        except Exception:
            deps = {}  # битый JSON — сигнал nodejs остаётся, deps-сигналы нет
        if "next" in deps:
            _add("nextjs")
        if "react" in deps:
            _add("react")

    if (root / "Dockerfile").is_file() or any(
        p.is_file() for p in root.glob("docker-compose*")
    ):
        _add("docker")
    if any(p.is_file() for p in root.glob("*.tf")):
        _add("terraform")
    if (root / "go.mod").is_file():
        _add("go")
    if (root / "Cargo.toml").is_file():
        _add("rust")
    if (root / ".claude").is_dir():
        _add("claude-code")
    return signals


def match_store(store_dir: Path, signals: list[str]) -> list[dict[str, Any]]:
    """Кандидаты из локального стора по сигналам проекта.

    Сканирует подпапки ``store_dir`` с ``_skill_meta.json``; навык матчится,
    если сигнал входит в ``manifest.tags`` (точное совпадение тега), является
    подстрокой slug'а или подстрокой ``manifest.description``
    (case-insensitive).

    Возвращает ``[{slug, version, signals}]`` — ``slug`` = идентичность папки
    стора (для slug-less навыка это числовой id), ``signals`` — чем
    заматчился (в порядке исходных сигналов).
    """
    root = Path(store_dir)
    out: list[dict[str, Any]] = []
    if not signals or not root.is_dir():
        return out
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        meta = read_meta(d)
        if meta is None:
            continue
        manifest = meta.get("manifest") or {}
        tags = {str(t).lower() for t in (manifest.get("tags") or [])}
        description = str(manifest.get("description") or "").lower()
        slug = str(meta.get("slug") or d.name)
        slug_lower = slug.lower()
        matched = [
            sig
            for sig in signals
            if (s := sig.lower()) in tags or s in slug_lower or (description and s in description)
        ]
        if matched:
            out.append(
                {"slug": slug, "version": meta.get("version"), "signals": matched}
            )
    return out


def merge_suggestions(
    local: list[dict[str, Any]],
    hub: list[dict[str, Any]],
    installed: set[str] | list[str] | dict[str, Any],
) -> list[dict[str, Any]]:
    """Объединяет local+hub кандидатов в единый список предложений.

    - дедуп по ``slug``; источник: ``local`` | ``hub`` | ``both``;
    - ``signals`` — объединение сигналов обоих источников (без дублей);
    - ``already=True`` для слагов из ``installed`` (уже включены в проект —
      идемпотентность: повторный onboard их не трогает).

    Порядок: сначала local-кандидаты (порядок стора), затем hub-only.
    """
    installed_set = {str(s) for s in installed}
    merged: dict[str, dict[str, Any]] = {}
    for source_name, items in (("local", local), ("hub", hub)):
        for item in items:
            slug = str(item.get("slug") or "")
            if not slug:
                continue
            entry = merged.get(slug)
            if entry is None:
                entry = {
                    "slug": slug,
                    "source": source_name,
                    "signals": [],
                    "already": slug in installed_set,
                }
                merged[slug] = entry
            elif entry["source"] != source_name:
                entry["source"] = "both"
            for sig in item.get("signals") or []:
                if sig not in entry["signals"]:
                    entry["signals"].append(sig)
    return list(merged.values())
