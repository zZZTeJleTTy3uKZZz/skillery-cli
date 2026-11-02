"""Продукт «Диктовка»: служба Windows + плагин Harness voice-input.

Версию службы даёт не выдумка, а реестр: ``voice-input.requires.dictation``
фиксирует «диктовка 1.10.0». Если реестр молчит, версия остаётся ``0.0.0``,
а причина попадает в пробелы.

Установщик собирается локально (ADR: «диктовке нужен CI-релиз установщика с
sha256»), поэтому ``url``/``sha256`` пусты. Аргументы тихой установки и способ
проверки взяты из ADR дословно.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .gaps import Gap

DICTATION_SLUG = "dictation"
SERVICE_KEY = "dictation-service"
DEFAULT_VERSION = "0.0.0"
INSTALLER_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES")
DETECT_FILE = "%LOCALAPPDATA%\\Programs\\Dictation\\dictation.exe"


def build_dictation_product(
    registry: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Gap]]:
    """Собрать продукт диктовки и пробелы, которые видит сам источник."""
    version = _service_version(registry)
    gaps: list[Gap] = []
    if version == DEFAULT_VERSION:
        gaps.append(
            Gap(
                section="version",
                product=DICTATION_SLUG,
                component=SERVICE_KEY,
                detail=DEFAULT_VERSION,
                reason="версия службы не объявлена в requires.dictation реестра",
            )
        )

    product = {
        "slug": DICTATION_SLUG,
        "title": "Диктовка",
        "version": version,
        "components": [
            {
                "key": SERVICE_KEY,
                "type": "service",
                "artifacts": [
                    {
                        "os": "windows",
                        "arch": "amd64",
                        "kind": "installer",
                        "url": "",
                        "sha256": "",
                        "install": {
                            "args": list(INSTALLER_ARGS),
                            "detect": {"file": DETECT_FILE},
                        },
                    }
                ],
            }
        ],
    }
    return product, gaps


def _service_version(registry: Mapping[str, Any]) -> str:
    plugins = registry.get("plugins")
    if not isinstance(plugins, list):
        return DEFAULT_VERSION
    for entry in plugins:
        if not isinstance(entry, Mapping) or entry.get("name") != "voice-input":
            continue
        requires = entry.get("requires")
        if isinstance(requires, Mapping):
            value = requires.get("dictation")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return DEFAULT_VERSION
