"""#2271: способность = плагин, и реестр у них ОДИН.

Способности объявлялись мимо хаба. Реальные навыки (telegram, vk-content-cli,
notebooklm, alice-chat-cli, boosty-content-cli) записывают плагины в
``[project.entry-points."skillery.plugins"]`` своего ``pyproject.toml``, а хаб
читал только ``[[capabilities]]`` из ``_skill_meta.toml`` — таких блоков не
было ни одного. Итог замерен на проде: ``capabilities = 0``,
``capability_leases = 0``. Весь эпик способностей обслуживал пустое множество.

Здесь проверяется решение: **источник правды — entry-points**, потому что
плагин, которого нет в ``pyproject.toml``, не существует физически (его не
найдёт ``PluginRegistry``), а манифест без entry-point — обещание без
исполнителя. Отсюда две противоположные по духу проверки:

* блока нет ⇒ реестр ВЫВОДИТСЯ из entry-points (а не остаётся пустым);
* блок есть ⇒ он обязан совпасть по множеству имён, иначе публикация падает
  с именем способности и файлом — ДО первого байта в сеть.

И одно свойство, без которого проверка была бы опасна: ``pyproject.toml``
читается, а не импортируется. Публикация не имеет права исполнять код
публикуемого навыка.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core.capability_manifest import (
    CapabilityManifestError,
    capabilities_of,
    plugin_entry_points_of,
    reconcile_capabilities,
)

TRANSCRIBER = "grok_transcriber"
ASK = "grok_ask"


def _meta(directory: Path, *capability_blocks: str) -> Path:
    body = ['description = "x"', 'version = "1.0.0"', *capability_blocks]
    path = directory / "_skill_meta.toml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _pyproject(directory: Path, **entry_points: str) -> Path:
    lines = ['[project]', 'name = "grok-chat"', 'version = "1.0.0"']
    if entry_points:
        lines.append('[project.entry-points."skillery.plugins"]')
        lines += [f'{name} = "{value}"' for name, value in entry_points.items()]
    path = directory / "pyproject.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Каноническая раскладка: навык в ``<repo>/skills/<name>/``, пакет — в корне."""
    (tmp_path / ".git").mkdir()
    skill_dir = tmp_path / "skills" / "grok"
    skill_dir.mkdir(parents=True)
    return tmp_path


class TestManifestIsDerived:
    def test_no_block_means_capabilities_come_from_entry_points(
        self, repo: Path
    ) -> None:
        """Ровно тот случай, что дал ``capabilities = 0`` на проде.

        Манифест есть, ``[[capabilities]]`` в нём нет, а плагины объявлены.
        Раньше хаб получал пустой список и способностей не заводил.
        """
        skill_dir = repo / "skills" / "grok"
        _meta(skill_dir)
        _pyproject(
            repo,
            **{
                TRANSCRIBER: "grok.capabilities.transcribe:GrokTranscriber",
                ASK: "grok.capabilities.ask:GrokAsk",
            },
        )
        assert capabilities_of(skill_dir) == [
            {
                "name": ASK,
                "entry_point": "grok.capabilities.ask:GrokAsk",
                "requires_lease": False,
            },
            {
                "name": TRANSCRIBER,
                "entry_point": "grok.capabilities.transcribe:GrokTranscriber",
                "requires_lease": False,
            },
        ]

    def test_skill_without_manifest_still_declares_its_plugins(
        self, tmp_path: Path
    ) -> None:
        """``boosty-content-cli``/``alice-chat-cli``: пакет в корне, манифеста нет."""
        _pyproject(tmp_path, **{ASK: "grok.capabilities.ask:GrokAsk"})
        assert [c["name"] for c in capabilities_of(tmp_path)] == [ASK]


class TestDivergenceIsAnError:
    def test_declared_capability_without_plugin(self, repo: Path) -> None:
        skill_dir = repo / "skills" / "grok"
        _meta(skill_dir, "[[capabilities]]", f'name = "{TRANSCRIBER}"')
        _pyproject(repo, **{ASK: "grok.capabilities.ask:GrokAsk"})
        with pytest.raises(CapabilityManifestError) as exc:
            capabilities_of(skill_dir)
        message = str(exc.value)
        assert TRANSCRIBER in message, "в ошибке обязано быть имя способности"
        assert "_skill_meta.toml" in message and "pyproject.toml" in message

    def test_plugin_without_declaration(self, repo: Path) -> None:
        skill_dir = repo / "skills" / "grok"
        _meta(skill_dir, "[[capabilities]]", f'name = "{ASK}"')
        _pyproject(
            repo,
            **{
                ASK: "grok.capabilities.ask:GrokAsk",
                TRANSCRIBER: "grok.capabilities.transcribe:GrokTranscriber",
            },
        )
        with pytest.raises(CapabilityManifestError) as exc:
            capabilities_of(skill_dir)
        assert TRANSCRIBER in str(exc.value)

    def test_matching_sets_pass_and_get_plugin_address(self, repo: Path) -> None:
        skill_dir = repo / "skills" / "grok"
        _meta(
            skill_dir,
            "[[capabilities]]",
            f'name = "{ASK}"',
            "requires_lease = true",
            'title = "Спросить"',
        )
        _pyproject(repo, **{ASK: "grok.capabilities.ask:GrokAsk"})
        assert capabilities_of(skill_dir) == [
            {
                "name": ASK,
                "title": "Спросить",
                "requires_lease": True,
                "entry_point": "grok.capabilities.ask:GrokAsk",
            }
        ]

    def test_scaffold_validation_reports_divergence(self, repo: Path) -> None:
        from skillery_cli.commands.scaffold import validate_scaffolded_skill

        skill_dir = repo / "skills" / "grok"
        (skill_dir / "SKILL.md").write_text(
            "---\nname: x\nversion: 1.0.0\ndescription: d\n---\n", encoding="utf-8"
        )
        _meta(skill_dir, "[[capabilities]]", f'name = "{TRANSCRIBER}"')
        _pyproject(repo, **{ASK: "grok.capabilities.ask:GrokAsk"})
        errors = validate_scaffolded_skill(skill_dir)
        assert any(TRANSCRIBER in e for e in errors), errors


class TestNoFalsePositives:
    def test_without_pyproject_declaration_is_taken_as_is(self, tmp_path: Path) -> None:
        """Навык-инструкция или папка вне дистрибутива: сверять не с чем."""
        _meta(tmp_path, "[[capabilities]]", f'name = "{TRANSCRIBER}"')
        assert capabilities_of(tmp_path) == [
            {"name": TRANSCRIBER, "requires_lease": False}
        ]

    def test_ascent_stops_at_repository_root(self, tmp_path: Path) -> None:
        """Чужой ``pyproject.toml`` выше корня репо навыку не приписывается.

        Без границы публикация папки, случайно оказавшейся внутри другого
        проекта, объявила бы хабу чужие плагины — и заняла бы под них
        глобально уникальные имена.
        """
        _pyproject(tmp_path, **{ASK: "someone.else:Ask"})
        repo = tmp_path / "inner"
        (repo / ".git").mkdir(parents=True)
        skill_dir = repo / "skills" / "grok"
        skill_dir.mkdir(parents=True)
        _meta(skill_dir)
        assert plugin_entry_points_of(skill_dir) == ({}, None)
        assert capabilities_of(skill_dir) == []

    def test_broken_pyproject_does_not_break_publish(self, repo: Path) -> None:
        skill_dir = repo / "skills" / "grok"
        _meta(skill_dir)
        (repo / "pyproject.toml").write_text("это = не = toml", encoding="utf-8")
        assert capabilities_of(skill_dir) == []


class TestReadNotImport:
    def test_pyproject_is_parsed_without_importing_the_package(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Плагин указывает на модуль, который взрывается при импорте.

        Если бы проверка поднимала дистрибутив (как это делает
        ``PluginRegistry`` в рантайме), публикация упала бы здесь. Она обязана
        читать TOML — приём ``adapterkit/testing/plugin.py``.
        """
        import builtins

        skill_dir = repo / "skills" / "grok"
        _meta(skill_dir)
        _pyproject(repo, **{ASK: "detonator:Boom"})

        real_import = builtins.__import__

        def _explode(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".")[0] == "detonator":
                raise AssertionError("публикация импортировала код навыка")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _explode)
        assert [c["name"] for c in capabilities_of(skill_dir)] == [ASK]


class TestReconcilePure:
    def test_empty_both_sides(self) -> None:
        assert reconcile_capabilities([], {}) == []

    def test_names_are_sorted_for_stable_publish_payload(self) -> None:
        derived = reconcile_capabilities(
            [], {"b": "m:B", "a": "m:A"}, pyproject_path=Path("pyproject.toml")
        )
        assert [c["name"] for c in derived] == ["a", "b"]
