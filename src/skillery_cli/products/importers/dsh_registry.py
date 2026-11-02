"""Импорт реестров dsh-plugins: ``registry.json`` и ``marketplace.json``.

Реестр — источник правды по плагинам Harness и Claude Code: имя, версия, путь
в дереве, ``requires`` и ``peerRange``. Marketplace перечисляет те же
Claude-плагины для установки через Claude Code, поэтому он используется как
перекрёстная проверка: плагин реестра, которого нет в marketplace, попадёт в
пробелы — иначе установка «через marketplace» молча не увидит его.

Плагины не публикуются архивами: реестр раздаёт их из локального дерева
(``plugins/<name>``, ``claude/<name>``). Поэтому артефакт — ``archive`` с
пустыми ``url``/``sha256``, а причина уходит в GAPS.md.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..manifest import validate_product
from .gaps import Gap

PLUGIN_GAP_REASON = (
    "архив плагина не публикуется: dsh-plugins раздаёт плагины из локального "
    "дерева, релиза с sha256 нет"
)


class RegistrySourceError(RuntimeError):
    """Источник реестра не читается или не тот, что ожидали."""


@dataclass(frozen=True)
class PluginSource:
    """Продукт плагина плюс его требования — разрешаются после слияния каталога."""

    product: dict[str, Any]
    requirements: tuple[tuple[str, str], ...]


def load_json(path: Path) -> dict[str, Any]:
    """Прочитать JSON реестра; ошибка называет файл, а не только путь."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RegistrySourceError(f"не найден {path.name}: {path}") from exc
    except OSError as exc:
        raise RegistrySourceError(f"не читается {path.name}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistrySourceError(f"{path.name}: невалидный JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistrySourceError(f"{path.name}: ожидался объект JSON")
    return data


def build_plugin_products(
    registry: Mapping[str, Any],
    marketplace: Mapping[str, Any] | None = None,
) -> tuple[list[PluginSource], list[Gap]]:
    """Собрать продукты плагинов и пробелы перекрёстной проверки."""
    plugins = _objects(registry, "plugins", "registry.json")
    claude_plugins = _objects(registry, "claudePlugins", "registry.json")
    gaps: list[Gap] = []

    sources = [_plugin_source(entry, plugin_type="plugin-dsh", os_name="windows")
               for entry in plugins]
    sources.extend(
        _plugin_source(entry, plugin_type="plugin-claude", os_name="linux")
        for entry in claude_plugins
    )

    if marketplace is not None:
        listed = {
            str(entry.get("name"))
            for entry in _objects(marketplace, "plugins", "marketplace.json")
        }
        for entry in claude_plugins:
            name = str(entry.get("name", "")).strip()
            if name and name not in listed:
                gaps.append(
                    Gap(
                        section="marketplace",
                        product=name,
                        component=name,
                        detail=name,
                        reason="плагин есть в registry.json (claudePlugins), "
                        "но не объявлен в marketplace.json",
                    )
                )

    for source in sources:
        errors = validate_product(source.product)
        if errors:  # pragma: no cover — защита от испорченного реестра
            raise RegistrySourceError(
                f"реестр дал негодный манифест {source.product.get('slug')!r}: "
                + "; ".join(errors)
            )
    return sources, gaps


def _plugin_source(
    entry: Mapping[str, Any], *, plugin_type: str, os_name: str
) -> PluginSource:
    name = _required_str(entry, "name")
    version = _required_str(entry, "version")
    path = str(entry.get("path", "")).strip()

    install: dict[str, Any] = {}
    if path:
        install["path"] = path
    peer_range = entry.get("peerRange")
    if isinstance(peer_range, str) and peer_range.strip():
        install["peer_range"] = peer_range

    requirements: list[tuple[str, str]] = []
    requires = entry.get("requires")
    if isinstance(requires, Mapping):
        requirements = [
            (str(key), str(value))
            for key, value in requires.items()
            if isinstance(value, str) and value.strip()
        ]

    product = {
        "slug": name,
        "title": name,
        "version": version,
        "components": [
            {
                "key": name,
                "type": plugin_type,
                "artifacts": [
                    {
                        "os": os_name,
                        "arch": "any",
                        "kind": "archive",
                        "url": "",
                        "sha256": "",
                        "install": install,
                    }
                ],
            }
        ],
    }
    return PluginSource(product=product, requirements=tuple(requirements))


def _objects(data: Mapping[str, Any], field: str, source: str) -> list[Mapping[str, Any]]:
    value = data.get(field, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RegistrySourceError(f"{source}: поле {field} должно быть списком")
    entries = [entry for entry in value if isinstance(entry, Mapping)]
    if len(entries) != len(value):
        raise RegistrySourceError(f"{source}: в {field} есть записи не-объекты")
    return entries


def _required_str(entry: Mapping[str, Any], field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RegistrySourceError(
            f"реестр: у плагина отсутствует обязательное поле {field!r}"
        )
    return value
