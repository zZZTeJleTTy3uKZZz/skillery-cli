"""Плагин Claude Code: папка в ``~/.claude/dsh-plugins`` + запись в env.

``~/.claude/settings.json`` — файл человека, и потерять его ключи нельзя:
правится ТОЛЬКО ``env.CLAUDE_CODE_PLUGIN_DIRS``, остальные ключи переносятся
байт в байт. Каталог плагинов называется в env один раз (родитель папок), как
делает ``dshplugins claude sync``: так новая папка подхватывается без
перезапуска сессии.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..plan import Artifact, Component
from .base import (
    ComponentResult,
    InstallContext,
    ProductError,
    install_archive,
    install_str,
    require_artifact,
)

PLUGIN_DIRS_VAR = "CLAUDE_CODE_PLUGIN_DIRS"


class ClaudePluginHandler:
    component_type = "plugin-claude"

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        return [
            f"скачать {require_artifact(component).url}",
            "проверить sha256",
            f"распаковать в {self._target(component, ctx)}",
            f"дописать {ctx.paths.claude_plugins} в {PLUGIN_DIRS_VAR}",
        ]

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        target = self._target(component, ctx)
        if not target.is_dir():
            return False
        dirs = _plugin_dirs(_load_settings(ctx.paths.claude_settings))
        return str(ctx.paths.claude_plugins) in dirs

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        target = self._target(component, ctx)
        settings = _load_settings(ctx.paths.claude_settings)
        dirs, dirs_changed = _normalized_dirs(settings, ctx.paths.claude_plugins)

        if target.is_dir() and not dirs_changed:
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="already-installed",
                path=str(target),
            )

        if not target.is_dir():
            install_archive(component, ctx, target, dir_name=target.name)

        if dirs_changed:
            settings.setdefault("env", {})[PLUGIN_DIRS_VAR] = os.pathsep.join(dirs)
            _save_settings(ctx.paths.claude_settings, settings)

        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="installed",
            path=str(target),
        )

    async def remove(self, component: Component, ctx: InstallContext) -> ComponentResult:
        target = self._target(component, ctx)
        existed = target.is_dir()
        if existed:
            shutil.rmtree(target)

        settings = _load_settings(ctx.paths.claude_settings)
        dirs = _plugin_dirs(settings)
        deploy = str(ctx.paths.claude_plugins)
        has_children = (
            ctx.paths.claude_plugins.is_dir()
            and any(item.is_dir() for item in ctx.paths.claude_plugins.iterdir())
        )
        if deploy in dirs and not has_children:
            dirs = [directory for directory in dirs if directory != deploy]
            settings.setdefault("env", {})[PLUGIN_DIRS_VAR] = os.pathsep.join(dirs)
            _save_settings(ctx.paths.claude_settings, settings)

        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="removed" if existed else "absent",
            path=str(target),
        )

    def _target(self, component: Component, ctx: InstallContext) -> Path:
        return ctx.paths.claude_plugins / self._name(component)

    def _name(self, component: Component) -> str:
        artifact = component.artifact
        if isinstance(artifact, Artifact):
            name = install_str(artifact, "name")
            if name:
                return name
        return component.key


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProductError(f"{path} не разобран как JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProductError(f"{path}: ожидался объект JSON")
    return data


def _save_settings(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _plugin_dirs(settings: dict[str, Any]) -> list[str]:
    env = settings.get("env")
    if not isinstance(env, dict):
        return []
    raw = env.get(PLUGIN_DIRS_VAR)
    if isinstance(raw, str) and raw.strip():
        return [item for item in raw.split(os.pathsep) if item.strip()]
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _normalized_dirs(
    settings: dict[str, Any], deploy_dir: Path
) -> tuple[list[str], bool]:
    """Оставить чужие папки, наши — схлопнуть в ОДНУ папку плагинов."""
    deploy = str(deploy_dir)
    current = _plugin_dirs(settings)
    normalized = [
        directory
        for directory in current
        if directory != deploy and Path(directory).parent != deploy_dir
    ]
    normalized.append(deploy)
    changed = normalized != current
    return normalized, changed
