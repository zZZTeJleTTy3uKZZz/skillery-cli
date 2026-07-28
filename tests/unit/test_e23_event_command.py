"""``skillery event track / queue / flush`` поверх ОБЩЕЙ очереди (#1180).

Своей очереди (``events.queue.json``) у аналитики больше нет — команды читают и
пишут общий outbox. HOME изолирован ``conftest.isolated_home``, поэтому путь
подменять не нужно: тест не завязан ни на env-имена кита, ни на его каталог.

Отдельно закреплено: ``event queue --clear`` трогает ТОЛЬКО аналитические
конверты. Очередь общая, и выбрасывать из неё чужие записи (логи CLI, запуски
навыков) команда про аналитику права не имеет.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from telemetrykit import outbox

from skillery_cli import output as output_module
from skillery_cli.commands import event as event_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import analytics_sync


def _text_mode() -> None:
    output_module._mode = "text"


def _kinds() -> list[str]:
    return [str(e.get("kind")) for e in outbox.read_batch(1000)]


def test_cmd_event_track_validates_event_type_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
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
    assert analytics_sync.pending_count() == 0


def test_cmd_event_track_rejects_invalid_json_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
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


def test_cmd_event_track_writes_to_shared_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    event_mod.cmd_event_track(
        event_type="skill.run",
        resource_type="skill",
        resource_id="my-skill",
        payload='{"slug": "my-skill", "duration_ms": 123}',
        metadata='{"source": "cli"}',
    )
    # Конверт лёг в ОБЩУЮ очередь под своим kind.
    assert _kinds() == [analytics_sync.KIND_ANALYTICS_EVENT]
    events = analytics_sync.pending()
    assert len(events) == 1
    assert events[0]["event_type"] == "skill.run"
    assert events[0]["payload"] == {"slug": "my-skill", "duration_ms": 123}
    assert events[0]["metadata"] == {"source": "cli"}
    assert events[0]["resource_id"] == "my-skill"


def test_no_second_queue_file_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Третья очередь запрещена: файла ``events.queue.json`` быть не должно."""
    _text_mode()
    event_mod.cmd_event_track(
        event_type="skill.run", resource_type=None, resource_id=None,
        payload=None, metadata=None,
    )
    assert not analytics_sync.legacy_queue_path().exists()


def test_cmd_event_queue_clear_touches_only_analytics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    analytics_sync.track("e.aaa")
    analytics_sync.track("e.bbb")
    outbox.append("log", {"level": "error", "message": "чужой конверт"})
    outbox.append("skill_run", {"skill": "atlas"})
    assert analytics_sync.pending_count() == 2

    event_mod.cmd_event_queue(show=False, clear=True)

    assert analytics_sync.pending_count() == 0
    assert sorted(_kinds()) == ["log", "skill_run"], (
        "очередь общая — чужие конверты выбрасывать нельзя"
    )


def test_cmd_event_queue_shows_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(output_module, "_mode", "json")
    analytics_sync.track("skill.enable", resource_type="skill", resource_id="7")
    outbox.append("log", {"level": "error", "message": "не наш"})

    event_mod.cmd_event_queue(show=True, clear=False)

    import json as _json

    p = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert p["size"] == 1, "считаем только аналитику"
    assert p["events"][0]["event_type"] == "skill.enable"
    assert p["queue_path"].endswith("outbox.jsonl")


def test_cmd_event_flush_delivers_through_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """flush = один проход воркера: аналитика уходит на /events и ack'ается."""
    from unittest.mock import MagicMock

    from skillery_cli.commands import _common

    _text_mode()
    analytics_sync.track("skill.run")

    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr("skillery_cli.config.load_tokens", lambda email: ("a", "r"))

    seen: list[list[dict]] = []
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        seen.append(list(events))
        return {"accepted": len(events)}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close
    fake_client.send_telemetry_batch = None
    monkeypatch.setattr(_common, "make_client", lambda cfg_, access: fake_client)

    event_mod.cmd_event_flush()

    assert len(seen) == 1 and seen[0][0]["event_type"] == "skill.run"
    assert analytics_sync.pending_count() == 0


def test_cmd_event_flush_breaks_the_throttle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ручная досылка идёт «прямо сейчас», а не ждёт окна воркера."""
    from unittest.mock import MagicMock

    from skillery_cli.commands import _common
    from skillery_cli.core import outbox_worker

    _text_mode()
    cfg = ClientConfig(base_url="http://x")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    calls: list[int] = []
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        calls.append(len(events))
        return {"accepted": len(events)}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close
    fake_client.send_telemetry_batch = None
    monkeypatch.setattr(_common, "make_client", lambda cfg_, access: fake_client)

    analytics_sync.track("skill.run")
    event_mod.cmd_event_flush()
    assert calls == [1]
    assert not outbox_worker.is_due(), "окно троттла закрылось"

    # Второй flush подряд — троттл пробит, событие уезжает сразу.
    analytics_sync.track("skill.run", payload={"n": 2})
    event_mod.cmd_event_flush()
    assert calls == [1, 1]
    assert analytics_sync.pending_count() == 0
