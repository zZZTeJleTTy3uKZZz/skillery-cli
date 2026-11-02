"""Каталог CLI-продуктов: факты о версиях, пакетах и релизах.

Реестра CLI в природе нет — у каждого инструмента свой способ доставки, и
придумывать URL/контрольные суммы здесь запрещено. Поэтому таблица ниже — это
зафиксированные факты на дату снимка (2026-11-02) с указанием источника:

- версия снята с установленной команды (``<cli> --version``) или с
  ``pyproject.toml``/``METADATA`` пакета;
- ``uv-tool`` — пакет, которым инструмент ставится (``uv tool install``);
- ``binary`` — ссылка на релиз, если формат ссылки известен и проверен;
- навык с тем же slug — только если он есть в хранилище Skillery
  (``~/.skillery/store/<slug>``); иначе ``skill=None`` и skill-ref не выдумываем.

Пробелы (нет пакета в публичном индексе, нет sha256, нет релиза) не
замалчиваются: они уходят в ``GAPS.md`` через ``GAP_REASONS``/``INDEX_NOTES``.
"""
from __future__ import annotations

from typing import Any

#: SHA256 артефактов line-cli v0.1.5 — из ``SHA256SUMS`` того же релиза
#: (github.com/zZZTeJleTTy3uKZZz/line-cli/releases/download/v0.1.5/SHA256SUMS).
_LINE_SHA256 = {
    ("linux", "amd64"): "d6ae7fb5c9d05f3de54accbe9c21a568c2b9978ed36904d1baa6a033890872b7",
    ("linux", "arm64"): "037d69c54ae0cd2211b7e3936c0ec3382c94d4a0af9a3519968c97226695b03d",
    ("windows", "amd64"): "dff959286b48afd034e8c808557f59241071ab4eb159086d7c2d3a273ed18911",
    ("macos", "amd64"): "edfc3fefa9356ced93d01ecca20fadca846faa7dd1556450478710c09e930afe",
    ("macos", "arm64"): "a10a4f1583972231b0227dc5c0f4e5196941ae1432e3cf88cc4026dfb923eaeb",
}

_LINE_ASSET = {
    "windows": "line_windows_amd64.zip",
    "linux": "line_linux_{arch}.tar.gz",
    "macos": "line_darwin_{arch}.tar.gz",
}

_ATLAS_ASSETS = {
    ("linux", "amd64"): "atlas-linux-amd64",
    ("linux", "arm64"): "atlas-linux-arm64",
    ("windows", "amd64"): "atlas-windows-amd64.exe",
    ("macos", "amd64"): "atlas-darwin-amd64",
    ("macos", "arm64"): "atlas-darwin-arm64",
}

#: Причина пробела для двоичных продуктов: у каждого своя, врать «нет данных»
#: нельзя — причина и есть ценность отчёта.
GAP_REASONS: dict[str, str] = {
    "atlas": (
        "ссылка собрана по формату издателя (api.atlas.pragmati.ru/dl/<asset>), "
        "но sha256 берётся из release.json релиза — локально его нет"
    ),
    "crew": (
        "релиза нет: CI crew ничего не публикует, бинарь ставится копированием "
        "собранного файла (ci/ и AGENTS.md репозитория crew)"
    ),
    "keyring": (
        "релиза нет: ставится из рабочей копии gitlab.com/cifropro1/kits/keyring "
        "скриптом install.sh, версия — коммит, semver не объявлен"
    ),
    "herdr": (
        "собственного релиза нет: herdr — форк herdrdev/herdr, ставится из "
        "локальной сборки; артефакты апстрима — чужой код, брать их нельзя"
    ),
}

#: Почему uv-tool нельзя поставить по имени из публичного индекса.
INDEX_NOTES: dict[str, str] = {
    "transcribe": (
        "пакета s-transcribe на PyPI нет (404): CLI ставится из локального "
        "каталога, нужен релиз в приватном индексе"
    ),
    "gateway": (
        "имя gateway на PyPI занято чужим пакетом 2.0; наш gateway 0.2.0 "
        "публикуется только из локального каталога — нужен приватный индекс"
    ),
    "beget": (
        "пакета beget-cli на PyPI нет (404): ставится из локального каталога"
    ),
    "b24": (
        "пакета bitrix24-content-cli на PyPI нет (404): ставится из локального "
        "каталога"
    ),
    "max": (
        "имя max-cli на PyPI занято чужим пакетом 1.2.1; наш max-cli 0.2.0 "
        "не опубликован"
    ),
    "gws": (
        "имя gws на PyPI занято чужим пакетом 0.0.1; наш gws 0.1.0 не опубликован"
    ),
}

def _uv_tool(package: str) -> dict[str, Any]:
    return {
        "os": "any",
        "arch": "any",
        "kind": "uv-tool",
        "install": {"package": package},
    }


def _opaque_binary(command: str) -> dict[str, Any]:
    """Бинарь без опубликованного релиза: платформа неизвестна, url пуст."""
    return {
        "os": "any",
        "arch": "any",
        "kind": "binary",
        "url": "",
        "sha256": "",
        "install": {"bin": command},
    }


#: Снимок фактов. Порядок задаёт порядок компонентов до сортировки.
CLI_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "slug": "atlas",
        "title": "Atlas — портфель и задачи",
        "version": "0.7.8",
        "skill": "atlas",
        "artifacts": [
            {
                "os": os_name,
                "arch": arch,
                "kind": "binary",
                "url": f"https://api.atlas.pragmati.ru/dl/{asset}",
                "sha256": "",
                "install": {"bin": "atlas.exe" if os_name == "windows" else "atlas"},
            }
            for (os_name, arch), asset in _ATLAS_ASSETS.items()
        ],
    },
    {
        "slug": "crew",
        "title": "crew — агенты и устройства",
        "version": "0.1.24",
        "skill": "crew",
        "artifacts": [_opaque_binary("crew")],
    },
    {
        "slug": "transcribe",
        "title": "transcribe — транскрибация",
        "version": "0.1.5",
        "skill": "transcribe",
        "artifacts": [_uv_tool("s-transcribe")],
    },
    {
        "slug": "gateway",
        "title": "gateway — оркестратор ИИ",
        "version": "0.2.0",
        "skill": "gateway",
        "artifacts": [_uv_tool("gateway")],
    },
    {
        "slug": "line",
        "title": "line — канал мессенджеров",
        "version": "0.1.5",
        "skill": "line",
        "artifacts": [
            {
                "os": os_name,
                "arch": arch,
                "kind": "binary",
                "url": (
                    "https://github.com/zZZTeJleTTy3uKZZz/line-cli/releases/download/"
                    f"v0.1.5/{_LINE_ASSET[os_name].format(arch=arch)}"
                ),
                "sha256": digest,
                "install": {"bin": "line.exe" if os_name == "windows" else "line"},
            }
            for (os_name, arch), digest in _LINE_SHA256.items()
        ],
    },
    {
        "slug": "beget",
        "title": "beget — хостинг Beget",
        "version": "0.1.0",
        "skill": "beget",
        "artifacts": [_uv_tool("beget-cli")],
    },
    {
        "slug": "b24",
        "title": "b24 — Битрикс24",
        "version": "0.5.10",
        # Навык со slug b24 в хранилище Skillery отсутствует (есть bitrix24).
        "skill": None,
        "artifacts": [_uv_tool("bitrix24-content-cli")],
    },
    {
        "slug": "skillery",
        "title": "Skillery CLI",
        "version": "0.5.94",
        "skill": "skillery",
        "artifacts": [_uv_tool("skillery-cli")],
    },
    {
        "slug": "max",
        "title": "max — мессенджер MAX",
        # Версия из pyproject.toml репозитория max-cli; установленной команды
        # на этой машине нет.
        "version": "0.2.0",
        "skill": None,  # навык называется max-cli, не max
        "artifacts": [_uv_tool("max-cli")],
    },
    {
        "slug": "gws",
        "title": "gws — Google Workspace",
        "version": "0.1.0",
        "skill": None,  # навык называется google-workspace
        "artifacts": [_uv_tool("gws")],
    },
    {
        "slug": "keyring",
        "title": "keyring — SSH-доступ к серверам",
        # Версия — коммит 39a3d7d; semver не объявлен, поэтому 0.0.0.
        "version": "0.0.0",
        # Навык keyring установлен вручную, в хранилище Skillery его нет.
        "skill": None,
        "artifacts": [_opaque_binary("keyring")],
    },
    {
        "slug": "herdr",
        "title": "herdr — терминальные пространства",
        "version": "0.9.5",
        "skill": None,
        "artifacts": [_opaque_binary("herdr")],
    },
)


def build_cli_products() -> list[dict[str, Any]]:
    """Собрать манифесты CLI-продуктов (детерминированно, без сети)."""
    products: list[dict[str, Any]] = []
    for entry in CLI_ENTRIES:
        components: list[dict[str, Any]] = [
            {
                "key": f"{entry['slug']}-cli",
                "type": "cli",
                "artifacts": [dict(artifact) for artifact in entry["artifacts"]],
            }
        ]
        skill = entry.get("skill")
        if skill:
            components.append(
                {
                    "key": f"{entry['slug']}-skill",
                    "type": "skill",
                    "artifacts": [
                        {
                            "os": "any",
                            "arch": "any",
                            "kind": "skill-ref",
                            "skill_ref": skill,
                        }
                    ],
                }
            )
        products.append(
            {
                "slug": entry["slug"],
                "title": entry["title"],
                "version": entry["version"],
                "components": components,
            }
        )
    return products
