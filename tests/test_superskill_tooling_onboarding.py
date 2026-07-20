"""#889: единый источник tooling-декларации + онбординг после установки.

- ``_merge_tooling_from_store`` — хаб-путь и локальный сходятся на
  ``_skill_meta.toml`` САМОГО навыка (у навыка с подпапкой бандл хаба мог
  прийти без cli/runtime_dependencies).
- ``_emit_onboarding`` — печатает «что делать дальше» из декларации навыка,
  чтобы ИИ-агент сам довёл настройку. Сторонний навык без декларации — молчит.
"""
from __future__ import annotations

from pathlib import Path

from skillery_cli.__main__ import _emit_onboarding, _merge_tooling_from_store

_META = """
description = "Atlas - PM"
version = "0.3.0"
kind = "tooling"
runtime_dependencies = [
    { kind = "pip", spec = "atlas-pm==0.3.0" },
]

[onboarding]
summary = "Atlas - локальный PM портфеля."
next_steps = ["atlas setup", "atlas task triage"]
docs = "https://example.invalid/readme"

[[cli]]
command_name = "atlas"
entrypoint = "atlas.cli:app"
"""


def _store(tmp_path: Path, meta: str | None) -> Path:
    d = tmp_path / "atlas"
    d.mkdir()
    (d / "SKILL.md").write_text("# Atlas\n", encoding="utf-8")
    if meta is not None:
        (d / "_skill_meta.toml").write_text(meta, encoding="utf-8")
    return d


class TestMergeToolingFromStore:
    def test_declaration_fills_empty_hub_manifest(self, tmp_path: Path) -> None:
        """Бандл хаба пуст (skill_path-навык) → берём из декларации навыка."""
        store = _store(tmp_path, _META)
        merged = _merge_tooling_from_store(
            {"version": "0.3.0", "cli": [], "runtime_dependencies": []}, store
        )
        assert merged["kind"] == "tooling"
        assert merged["runtime_dependencies"][0]["spec"] == "atlas-pm==0.3.0"
        assert merged["cli"][0]["command_name"] == "atlas"
        assert merged["onboarding"]["next_steps"] == [
            "atlas setup",
            "atlas task triage",
        ]
        # не-tooling поля исходного манифеста сохранены
        assert merged["version"] == "0.3.0"

    def test_third_party_skill_without_declaration_untouched(
        self, tmp_path: Path
    ) -> None:
        """Сторонний навык (только SKILL.md) → никаких tooling-полей."""
        store = _store(tmp_path, None)
        merged = _merge_tooling_from_store({"version": "1.0.0"}, store)
        assert merged == {"version": "1.0.0"}
        for key in ("kind", "cli", "runtime_dependencies", "onboarding"):
            assert key not in merged

    def test_no_store_dir_is_safe(self) -> None:
        assert _merge_tooling_from_store({"a": 1}, None) == {"a": 1}
        assert _merge_tooling_from_store(None, None) == {}

    def test_broken_toml_does_not_break_install(self, tmp_path: Path) -> None:
        store = _store(tmp_path, "это = не ( валидный TOML")
        assert _merge_tooling_from_store({"v": 1}, store) == {"v": 1}


class TestEmitOnboarding:
    def test_prints_next_steps(self, monkeypatch) -> None:
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "skillery_cli.__main__.emit_message",
            lambda text, **kw: seen.append((text, kw)),
        )
        _emit_onboarding(
            {
                "onboarding": {
                    "summary": "Atlas - PM",
                    "next_steps": ["atlas setup", "atlas task triage"],
                    "docs": "https://example.invalid/readme",
                }
            },
            slug="atlas",
        )
        assert len(seen) == 1
        text, kw = seen[0]
        assert "Что дальше" in text
        assert "1. atlas setup" in text
        assert "2. atlas task triage" in text
        # структурно — для агента (в json-режиме уходит в stderr полями)
        assert kw["next_steps"] == ["atlas setup", "atlas task triage"]
        assert kw["skill"] == "atlas"

    def test_silent_without_declaration(self, monkeypatch) -> None:
        seen: list = []
        monkeypatch.setattr(
            "skillery_cli.__main__.emit_message",
            lambda text, **kw: seen.append(text),
        )
        _emit_onboarding({"version": "1.0.0"}, slug="third-party")
        _emit_onboarding(None, slug="none")
        _emit_onboarding({"onboarding": {}}, slug="empty")
        assert seen == []
