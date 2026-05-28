"""Тесты ``skills-hub event track / queue / flush`` (E23).

Эти команды редактируют локальную очередь — fixture'ируем её через
override ``default_queue_path`` функцией.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skills_hub_cli import output as output_module
from skills_hub_cli.commands import event as event_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.daemon.event_collector import EventCollector


def _text_mode() -> None:
    output_module._mode = "text"


def _override_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    queue_path = tmp_path / "events.queue.json"
    monkeypatch.setattr(event_mod, "default_queue_path", lambda: queue_path)
    return queue_path


def test_cmd_event_track_validates_event_type_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    _override_queue(monkeypatch, tmp_path)
    import typer

    with pytest.raises(typer.Exit) as exc:
        event_mod.cmd_event_track(
            event_type="x",  # < 3 chars
            resource_type=None,
            resource_id=None,
            payload=None,
            metadata=None,
        )
    assert exc.value.exit_code == 1


def test_cmd_event_track_rejects_invalid_json_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    _override_queue(monkeypatch, tmp_path)
    import typer

    with pytest.raises(typer.Exit) as exc:
        event_mod.cmd_event_track(
            event_type="skill.run",
            resource_type=None,
            resource_id=None,
            payload="{not-json",
            metadata=None,
        )
    assert exc.value.exit_code == 1


def test_cmd_event_track_writes_to_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    queue_path = _override_queue(monkeypatch, tmp_path)
    event_mod.cmd_event_track(
        event_type="skill.run",
        resource_type="skill",
        resource_id="my-skill",
        payload='{"slug": "my-skill", "duration_ms": 123}',
        metadata='{"source": "cli"}',
    )
    coll = EventCollector(queue_path)
    events = coll.peek()
    assert len(events) == 1
    assert events[0].event_type == "skill.run"
    assert events[0].payload == {"slug": "my-skill", "duration_ms": 123}
    assert events[0].metadata == {"source": "cli"}


def test_cmd_event_queue_clear_resets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    queue_path = _override_queue(monkeypatch, tmp_path)
    coll = EventCollector(queue_path)
    coll.append("e.a")
    coll.append("e.b")
    assert coll.size() == 2
    event_mod.cmd_event_queue(show=False, clear=True)
    assert coll.size() == 0


def test_cmd_event_flush_calls_sender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """flush должен прозвонить sender один раз и вернуть SendResult."""
    from skills_hub_cli.commands import _common
    from unittest.mock import MagicMock

    _text_mode()
    queue_path = _override_queue(monkeypatch, tmp_path)
    coll = EventCollector(queue_path)
    coll.append("skill.run")

    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))

    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        return {"accepted": len(events), "event_ids": [f"ev_{i}" for i in range(len(events))]}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close

    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)

    event_mod.cmd_event_flush()
    # Очередь должна быть пуста
    assert coll.size() == 0
