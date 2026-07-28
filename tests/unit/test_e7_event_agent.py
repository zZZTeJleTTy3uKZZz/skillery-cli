"""E7 аналитика — `track_skill_event` прокидывает `agent` в payload.

Какой ИИ-агент (claude_code / codex / antigravity / generic) ставит/включает
навык — отдельная ось аналитики (план E7). Кладётся в ``payload["agent"]``,
рядом со ``scope`` и ``source``. ``None`` (или не передан) → ключа в payload нет
(не шумим). НЕ путать с ``metadata.source="cli"`` (транспортный канал).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core import analytics_sync
from skillery_cli.daemon import instrumentation as instr


class _Queue:
    """Чтение аналитики из ОБЩЕГО outbox'а (#1180: своей очереди больше нет).

    HOME изолирован ``conftest.isolated_home``, поэтому outbox уже лежит в
    tmp — подменять пути не требуется, а значит тест не завязан на имена env
    кита (они меняются при его нейтрализации).
    """

    @staticmethod
    def peek() -> list[dict]:
        return analytics_sync.pending()

    @staticmethod
    def size() -> int:
        return analytics_sync.pending_count()


def _capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Queue:
    return _Queue()


def test_track_skill_event_puts_agent_in_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    coll = _capture(monkeypatch, tmp_path)
    instr.track_skill_event(
        "skill.install", slug="bitrix24", scope="project",
        source="hub", agent="claude_code",
    )
    ev = coll.peek()[0]
    assert ev["event_type"] == "skill.install"
    assert ev["payload"]["agent"] == "claude_code"
    # agent — рядом со scope/source, всё в payload.
    assert ev["payload"]["scope"] == "project"
    assert ev["payload"]["source"] == "hub"
    # metadata.source — транспорт (cli), НЕ агент.
    assert ev["metadata"] == {"source": "cli"}


def test_track_skill_event_agent_none_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """agent=None (или не передан) → ключа в payload нет (не шумим)."""
    coll = _capture(monkeypatch, tmp_path)
    instr.track_skill_event("skill.uninstall", slug="x", scope="global")
    ev = coll.peek()[0]
    assert "agent" not in ev["payload"]


@pytest.mark.parametrize(
    "agent", ["claude_code", "codex", "antigravity", "opencode", "generic"]
)
def test_track_skill_event_all_agent_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    coll = _capture(monkeypatch, tmp_path)
    instr.track_skill_event(
        "skill.enable", slug="s", scope="project", agent=agent
    )
    assert coll.peek()[0]["payload"]["agent"] == agent
