"""Плагин DeepSeek Harness: папка в ``~/.dsh/plugins`` + строки в раскладке.

Логика — из ``dsh-plugins/install.ps1``: плагин объявляет свою запись
(``plugin.json → id``) и записи-спутники (``insert``), которые нужны ему для
работы (например, службы speech-to-text у voice-input). Все они пишутся в блок
``insert:`` файла ``~/.dsh/profiles/<профиль>/cordis.patch.yml``;

* ``${pluginDir}`` в конфиге разворачивается в папку установленного плагина;
* правка идёт по id — повторная установка не добавляет вторую строку;
* чужие строки и комментарии переносятся без изменений.
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..plan import Component
from .base import (
    ComponentResult,
    InstallContext,
    ProductError,
    install_archive,
    install_str,
)
from .insert_block import insert_block_ids, update_insert_block


class DshPluginHandler:
    component_type = "plugin-dsh"

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        target = self._target(component, ctx)
        return [
            f"распаковать плагин в {target}",
            f"вписать id плагина в {ctx.paths.dsh_patch}",
        ]

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        target = self._target(component, ctx)
        if not (target / "plugin.json").is_file():
            return False
        manifest = _read_manifest(target)
        ids = _manifest_ids(manifest)
        if not ids:
            return False
        patch = ctx.paths.dsh_patch
        if not patch.is_file():
            return False
        present = set(insert_block_ids(patch.read_text(encoding="utf-8")))
        return all(row_id in present for row_id in ids)

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        target = self._target(component, ctx)
        if self.verify(component, ctx):
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="already-installed",
                path=str(target),
            )

        if not (target / "plugin.json").is_file():
            install_archive(component, ctx, target, dir_name=target.name)

        manifest = _read_manifest(target)
        rows = _rows(manifest, target)
        patch = ctx.paths.dsh_patch
        text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
        updated = update_insert_block(text, rows)
        if updated != text or not patch.is_file():
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text(updated, encoding="utf-8", newline="\n")

        present = set(insert_block_ids(updated))
        missing = [str(row.get("id")) for row in rows if str(row.get("id")) not in present]
        if missing:
            raise ProductError(
                f"плагин {component.key}: строки {', '.join(missing)} не попали в раскладку"
            )
        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="installed",
            path=str(target),
        )

    async def remove(self, component: Component, ctx: InstallContext) -> ComponentResult:
        target = self._target(component, ctx)
        existed = (target / "plugin.json").is_file()
        ids: list[str] = []
        if existed:
            ids = _manifest_ids(_read_manifest(target))

        patch = ctx.paths.dsh_patch
        if ids and patch.is_file():
            text = patch.read_text(encoding="utf-8")
            updated = update_insert_block(text, [], remove_ids=ids)
            if updated != text:
                patch.write_text(updated, encoding="utf-8", newline="\n")
        if target.exists():
            shutil.rmtree(target)

        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="removed" if existed else "absent",
            path=str(target),
        )

    def _target(self, component: Component, ctx: InstallContext) -> Path:
        return ctx.paths.dsh_plugins / _plugin_name(component)


def _plugin_name(component: Component) -> str:
    artifact = component.artifact
    if artifact is not None:
        name = install_str(artifact, "name")
        if name:
            return name
    return component.key


def _read_manifest(target: Path) -> Mapping[str, Any]:
    path = target / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductError(f"манифест плагина не читается ({path}): {exc}") from exc
    if not isinstance(data, Mapping):
        raise ProductError(f"манифест плагина {path}: ожидался объект JSON")
    return data


def _manifest_ids(manifest: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    own = manifest.get("id")
    if isinstance(own, str) and own.strip():
        ids.append(own)
    insert = manifest.get("insert")
    if isinstance(insert, list):
        for row in insert:
            if isinstance(row, Mapping) and isinstance(row.get("id"), str):
                ids.append(str(row["id"]))
    return ids


def _rows(manifest: Mapping[str, Any], target: Path) -> list[dict[str, Any]]:
    own_id = manifest.get("id")
    if not isinstance(own_id, str) or not own_id.strip():
        raise ProductError("манифест плагина не объявляет id")
    entry = manifest.get("entry")
    entry_name = entry if isinstance(entry, str) and entry.strip() else "index.js"
    rows: list[dict[str, Any]] = [
        {"id": own_id, "name": _file_url(target / entry_name)}
    ]
    insert = manifest.get("insert")
    if isinstance(insert, list):
        for raw_row in insert:
            if not isinstance(raw_row, Mapping):
                continue
            row = {str(key): _expand(value, target) for key, value in raw_row.items()}
            rows.append(row)
    return rows


def _expand(value: Any, plugin_dir: Path) -> Any:
    if isinstance(value, str):
        return value.replace("${pluginDir}", plugin_dir.as_posix()).replace(
            "$pluginDir", plugin_dir.as_posix()
        )
    if isinstance(value, Mapping):
        return {str(key): _expand(item, plugin_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, plugin_dir) for item in value]
    return value


def _file_url(path: Path) -> str:
    posix = path.as_posix()
    if posix.startswith("/"):
        return "file://" + posix
    return "file:///" + posix
