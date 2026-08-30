"""#1268/#2456: зеркало разбора ``[[capabilities]]`` понимает группу и setup.

Бэкенд принимает у способности ещё два поля — ``entry_point_group`` (в какой
группе entry-point'ов живёт плагин) и ``setup`` (шаг, без которого способность
не заработает после установки). Гейт публикации в CLI про них не знал и падал
с «неизвестные ключи»: автор, написавший в манифест ровно то, что ждёт хаб, не
мог опубликовать версию вовсе, а единственным «лечением» было УБРАТЬ поля.

Два разбора одной сущности обязаны совпадать — иначе локальная проверка
запрещает то, что сервер разрешает, и наоборот.

Плюс собственная работа гейта: раз группа названа, её надо СВЕРИТЬ с
``pyproject.toml``. Потребитель резолвит способность по паре (группа, имя) — в
чужой группе он её не найдёт, и молчаливо опубликованная версия оказалась бы
нерабочей ровно там, ради чего её и публиковали.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core.capability_manifest import (
    KNOWN_KEYS,
    CapabilityManifestError,
    capabilities_of,
    entry_point_groups_of,
    parse_capabilities,
)

TRANSCRIBER = "grok_transcriber"
FOREIGN_GROUP = "gateway.channels"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Каноническая раскладка: навык в ``<repo>/skills/<name>/``, пакет в корне."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "skills" / "grok").mkdir(parents=True)
    return tmp_path


def _meta(directory: Path, *lines: str) -> Path:
    body = ['description = "x"', 'version = "1.0.0"', *lines]
    path = directory / "_skill_meta.toml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _pyproject(directory: Path, groups: dict[str, dict[str, str]]) -> Path:
    lines = ["[project]", 'name = "grok-chat"', 'version = "1.0.0"']
    for group, table in groups.items():
        lines.append(f'[project.entry-points."{group}"]')
        lines += [f'{name} = "{value}"' for name, value in table.items()]
    path = directory / "pyproject.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ==================== поля, которые ждёт бэкенд ==========================
class TestKeysMirrorBackend:
    def test_group_and_setup_are_known_keys(self) -> None:
        """Ровно те имена, что у ``CapabilityDecl`` на бэкенде."""
        assert {"entry_point_group", "setup"} <= KNOWN_KEYS

    def test_declaration_with_group_and_setup_survives_parsing(self) -> None:
        caps = parse_capabilities(
            {
                "capabilities": [
                    {
                        "name": TRANSCRIBER,
                        "entry_point": "grok.capabilities.transcribe:T",
                        "entry_point_group": FOREIGN_GROUP,
                        "setup": "нужен ключ GROK_API_KEY",
                    }
                ]
            }
        )
        assert caps == [
            {
                "name": TRANSCRIBER,
                "entry_point": "grok.capabilities.transcribe:T",
                "entry_point_group": FOREIGN_GROUP,
                "setup": "нужен ключ GROK_API_KEY",
                "requires_lease": False,
            }
        ]


# ==================== сверка группы с pyproject.toml =====================
class TestGroupIsVerified:
    def test_own_group_resolves_address_from_that_group(self, repo: Path) -> None:
        """Адрес берётся из НАЗВАННОЙ группы, а не из ``skillery.plugins``.

        Подставить сюда адрес дефолтной группы значило бы подменить
        исполнителя способности.
        """
        skill_dir = repo / "skills" / "grok"
        _meta(
            skill_dir,
            "[[capabilities]]",
            f'name = "{TRANSCRIBER}"',
            f'entry_point_group = "{FOREIGN_GROUP}"',
        )
        _pyproject(
            repo,
            {FOREIGN_GROUP: {TRANSCRIBER: "grok.channels.transcribe:T"}},
        )
        assert capabilities_of(skill_dir) == [
            {
                "name": TRANSCRIBER,
                "entry_point_group": FOREIGN_GROUP,
                "entry_point": "grok.channels.transcribe:T",
                "requires_lease": False,
            }
        ]

    def test_unknown_group_names_the_problem(self, repo: Path) -> None:
        """Группы нет в дистрибутиве — публикация падает ДО сети."""
        skill_dir = repo / "skills" / "grok"
        _meta(
            skill_dir,
            "[[capabilities]]",
            f'name = "{TRANSCRIBER}"',
            'entry_point_group = "opechatka.plugins"',
        )
        _pyproject(
            repo,
            {FOREIGN_GROUP: {TRANSCRIBER: "grok.channels.transcribe:T"}},
        )
        with pytest.raises(CapabilityManifestError) as exc:
            capabilities_of(skill_dir)
        assert "opechatka.plugins" in str(exc.value)
        assert FOREIGN_GROUP in str(exc.value), "не сказано, какие группы есть"

    def test_name_missing_in_declared_group_is_an_error(self, repo: Path) -> None:
        """Группа есть, а плагина в ней нет — обещание без исполнителя."""
        skill_dir = repo / "skills" / "grok"
        _meta(
            skill_dir,
            "[[capabilities]]",
            f'name = "{TRANSCRIBER}"',
            f'entry_point_group = "{FOREIGN_GROUP}"',
        )
        _pyproject(repo, {FOREIGN_GROUP: {"grok_ask": "grok.channels.ask:A"}})
        with pytest.raises(CapabilityManifestError) as exc:
            capabilities_of(skill_dir)
        assert TRANSCRIBER in str(exc.value)

    def test_foreign_group_is_not_demanded_from_default_group(
        self, repo: Path
    ) -> None:
        """Чужая группа не участвует в сверке с ``skillery.plugins``.

        Иначе честно объявленная способность выглядела бы как «не объявлена»
        сразу с двух сторон: и как лишняя в блоке, и как отсутствующая среди
        плагинов.
        """
        skill_dir = repo / "skills" / "grok"
        _meta(
            skill_dir,
            "[[capabilities]]",
            f'name = "{TRANSCRIBER}"',
            f'entry_point_group = "{FOREIGN_GROUP}"',
        )
        _pyproject(
            repo,
            {
                FOREIGN_GROUP: {TRANSCRIBER: "grok.channels.transcribe:T"},
                "skillery.plugins": {"grok_ask": "grok.capabilities.ask:A"},
            },
        )
        # ``grok_ask`` живёт в дефолтной группе и в блоке не объявлен — но
        # блок описывает ДРУГУЮ группу, и требовать его здесь неоткуда.
        caps = capabilities_of(skill_dir)
        assert [c["name"] for c in caps] == [TRANSCRIBER]


# ==================== чтение групп ======================================
class TestEntryPointGroups:
    def test_reads_every_group_of_the_distribution(self, repo: Path) -> None:
        _pyproject(
            repo,
            {
                "skillery.plugins": {"grok_ask": "grok.capabilities.ask:A"},
                FOREIGN_GROUP: {TRANSCRIBER: "grok.channels.transcribe:T"},
            },
        )
        groups, path = entry_point_groups_of(repo / "skills" / "grok")
        assert path is not None
        assert set(groups) == {"skillery.plugins", FOREIGN_GROUP}
        assert groups[FOREIGN_GROUP][TRANSCRIBER] == "grok.channels.transcribe:T"

    def test_no_pyproject_means_nothing_to_check(self, tmp_path: Path) -> None:
        groups, path = entry_point_groups_of(tmp_path)
        assert groups == {} and path is None
