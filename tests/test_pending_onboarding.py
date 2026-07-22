"""#1059: onboarding фоновой (демоном) установки — в журнал, виден в `status`.

Демон headless (stdout→DEVNULL), поэтому напечатанный `_emit_onboarding` блок
«что дальше» терялся — агент не узнавал, что поставилось в фоне и что доделать.
Теперь при headless он пишется в pending-onboarding.json, а `skillery status`
показывает и вычищает.
"""
from __future__ import annotations

import json

import pytest

from skillery_cli import __main__ as m

_MANIFEST = {
    "onboarding": {
        "summary": "Atlas — PM портфеля.",
        "next_steps": ["atlas config set projects_root <path>", "atlas project init"],
        "docs": "https://example/readme",
    }
}


@pytest.fixture
def pending_file(tmp_path, monkeypatch):
    p = tmp_path / "pending-onboarding.json"
    monkeypatch.setattr(m, "_pending_onboarding_path", lambda: p)
    return p


class TestHeadlessPersists:
    def test_headless_writes_journal_not_console(self, pending_file, capsys):
        """Демон: onboarding в журнал, НЕ в консоль (она бы всё равно потерялась)."""
        m._emit_onboarding(_MANIFEST, slug="atlas", headless=True)

        out = capsys.readouterr()
        assert "Что дальше" not in (out.out + out.err), "headless не должен печатать"
        items = json.loads(pending_file.read_text(encoding="utf-8"))["items"]
        assert len(items) == 1
        assert items[0]["slug"] == "atlas"
        assert items[0]["next_steps"] == _MANIFEST["onboarding"]["next_steps"]
        assert items[0]["summary"] == "Atlas — PM портфеля."

    def test_interactive_does_not_persist(self, pending_file):
        """Интерактивная установка печатает сразу — в журнал НЕ пишет."""
        m._emit_onboarding(_MANIFEST, slug="atlas", headless=False)
        assert not pending_file.exists(), "agent уже увидел вывод, журнал не нужен"

    def test_no_onboarding_declaration_is_noop(self, pending_file):
        """Навык без [onboarding] — ничего не пишем даже в headless."""
        m._emit_onboarding({"foo": "bar"}, slug="x", headless=True)
        assert not pending_file.exists()


class TestJournalMechanics:
    def test_dedupe_by_slug_keeps_latest(self, pending_file):
        """Повторная фоновая установка того же навыка вытесняет прежнюю запись."""
        m._record_pending_onboarding({"slug": "a", "next_steps": ["v1"]})
        m._record_pending_onboarding({"slug": "a", "next_steps": ["v2"]})
        m._record_pending_onboarding({"slug": "b", "next_steps": ["x"]})

        items = m._read_pending_onboarding()
        assert len(items) == 2
        a = next(i for i in items if i["slug"] == "a")
        assert a["next_steps"] == ["v2"]

    def test_read_then_clear(self, pending_file):
        m._record_pending_onboarding({"slug": "a"})
        assert m._read_pending_onboarding()
        m._clear_pending_onboarding()
        assert m._read_pending_onboarding() == []

    def test_read_robust_to_corrupt_file(self, pending_file):
        pending_file.write_text("{ битый json", encoding="utf-8")
        assert m._read_pending_onboarding() == []
