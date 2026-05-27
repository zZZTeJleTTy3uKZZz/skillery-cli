"""Установщик skills: git clone (если есть repo) + write meta.

Поддерживает global и per-project scope (через IAgentTarget.slug_dir(project=...)).
Перепринимает все основные методы с project: Path | None.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skills_hub_cli.core.agents.base import IAgentTarget
from skills_hub_cli.core.skill_filter import apply_skill_filter


def _on_rm_error(func, path, exc_info):  # noqa: ANN001
    """Чинит read-only-файлы в Windows (.git/objects/...) перед удалением."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _force_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_on_rm_error)

_SKILL_META_FILE = "_skill_meta.json"


def _authenticated_url(url: str) -> str:
    if "@" in url or not url.startswith("https://"):
        return url
    token = os.environ.get("SKILLS_HUB_GIT_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if not token:
        return url
    return url.replace("https://", f"https://oauth2:{token}@", 1)


@dataclass
class InstallResult:
    slug: str
    version: str
    target_dir: Path
    is_update: bool
    scope: str  # "global" | "project"
    filter_result: dict[str, int] | None = None  # {"removed": N, "kept": M} после filter


def write_meta(slug_dir: Path, meta: dict[str, Any]) -> None:
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / _SKILL_META_FILE).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def read_meta(slug_dir: Path) -> dict[str, Any] | None:
    p = slug_dir / _SKILL_META_FILE
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


class SkillInstaller:
    """MVP: install через git clone + write meta. Scope = global | project."""

    def __init__(self, target: IAgentTarget) -> None:
        self._target = target

    def install(
        self,
        *,
        slug: str,
        version: str,
        commit_sha: str,
        repo_url: str | None,
        manifest: dict[str, Any],
        project: Path | None = None,
        force: bool = False,
    ) -> InstallResult:
        scope = "project" if project is not None else "global"
        slug_dir = self._target.slug_dir(slug, project=project)
        has_our_meta = slug_dir.exists() and read_meta(slug_dir) is not None
        is_foreign = slug_dir.exists() and not has_our_meta

        if is_foreign and not force:
            raise RuntimeError(
                f"Папка {slug_dir} уже существует и не управляется skills-hub "
                "(нет _skill_meta.json). Перезаписать через --force "
                "(удалит содержимое!) или удалить вручную."
            )

        is_update = has_our_meta
        if is_update:
            write_meta(
                slug_dir,
                self._build_meta(slug, version, commit_sha, manifest, scope, project),
            )
            return InstallResult(
                slug=slug,
                version=version,
                target_dir=slug_dir,
                is_update=True,
                scope=scope,
            )

        # Чистая установка (или force перезатирает foreign-папку)
        if is_foreign and force:
            _force_rmtree(slug_dir)

        slug_dir.parent.mkdir(parents=True, exist_ok=True)
        filter_result: dict[str, int] | None = None
        if repo_url:
            ref = f"v{version}"
            url = _authenticated_url(repo_url)
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", ref, url, str(slug_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or e.stdout or "").replace(
                    os.environ.get("SKILLS_HUB_GIT_TOKEN", "***"), "***"
                ).replace(os.environ.get("GITLAB_TOKEN", "***"), "***")
                raise RuntimeError(f"git clone failed: {stderr}") from e
            # Применяем dev/release фильтр после clone:
            #   .skillignore — удаляет matched (dev-файлы);
            #   SKILL.md `files:` allowlist — оставляет только matched.
            filter_result = apply_skill_filter(slug_dir)
        else:
            slug_dir.mkdir(parents=True, exist_ok=True)
            (slug_dir / "SKILL.md").write_text(
                f"---\nname: {slug}\nversion: {version}\n---\n\n# {slug}\n\n"
                "Stub — установлено без git репо.\n",
                encoding="utf-8",
            )
        write_meta(
            slug_dir,
            self._build_meta(slug, version, commit_sha, manifest, scope, project),
        )
        return InstallResult(
            slug=slug,
            version=version,
            target_dir=slug_dir,
            is_update=False,
            scope=scope,
            filter_result=filter_result,
        )

    def _build_meta(
        self,
        slug: str,
        version: str,
        commit_sha: str,
        manifest: dict[str, Any],
        scope: str,
        project: Path | None,
    ) -> dict[str, Any]:
        return {
            "slug": slug,
            "version": version,
            "commit_sha": commit_sha,
            "manifest": manifest,
            "agent": self._target.name,
            "scope": scope,
            "project": str(project) if project else None,
        }
