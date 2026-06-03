"""Проектный манифест набора навыков: <project>/.skills-hub/skills.toml.

Формат:
    [skills]
    bitrix24 = "*"      # "*" = latest из стора (версии-пины — на будущее)
    wb-api   = "*"

Коммитится в git → набор воспроизводим командой `skills-hub sync`.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w

MANIFEST_REL = Path(".skills-hub") / "skills.toml"


def manifest_path(project: Path) -> Path:
    return Path(project) / MANIFEST_REL


def load(project: Path) -> dict[str, str]:
    """{slug: version_spec}. Пустой dict если манифеста нет."""
    p = manifest_path(project)
    if not p.exists():
        return {}
    # utf-8-sig — толерантность к BOM (как ClientConfig.load).
    data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    skills = data.get("skills") or {}
    return {str(k): str(v) for k, v in skills.items()}


def save(project: Path, skills: dict[str, str]) -> None:
    p = manifest_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Сортируем ключи для стабильного git-diff.
    ordered = {k: skills[k] for k in sorted(skills)}
    p.write_text(tomli_w.dumps({"skills": ordered}), encoding="utf-8")


def add(project: Path, slug: str, version: str = "*") -> None:
    skills = load(project)
    skills[slug] = version
    save(project, skills)


def remove(project: Path, slug: str) -> bool:
    skills = load(project)
    if slug not in skills:
        return False
    del skills[slug]
    save(project, skills)
    return True


def list_(project: Path) -> list[str]:
    return sorted(load(project).keys())
