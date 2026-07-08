"""Тесты `EventSender` (E23): успешная отправка, requeue при сбое,
стабильный idempotency-key.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli.core.transport import ApiError
from skillery_cli.daemon.event_collector import EventCollector
from skillery_cli.daemon.event_sender import EventSender


@pytest.mark.asyncio
async def test_sender_send_once_empty_queue(tmp_path: Path) -> None:
    collector = EventCollector(tmp_path / "q.json")
    client = MagicMock()
    factory = MagicMock(return_value=client)
    sender = EventSender(collector, factory)
    result = await sender.send_once()
    assert result.sent == 0
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_sender_happy_path_drains_and_posts(tmp_path: Path) -> None:
    collector = EventCollector(tmp_path / "q.json")
    collector.append("skill.run", payload={"slug": "a"})
    collector.append("skill.install", payload={"slug": "b"})

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        captured["events"] = events
        captured["idem"] = idempotency_key
        return {"accepted": len(events), "event_ids": [f"ev_{i}" for i in range(len(events))]}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close

    sender = EventSender(collector, lambda: fake_client)
    result = await sender.send_once()
    assert result.sent == 2
    assert result.accepted == 2
    assert result.requeued == 0
    # Очередь опустошена
    assert collector.size() == 0
    # Idempotency-key стабильный
    assert captured["idem"].startswith("sh-cli-")


@pytest.mark.asyncio
async def test_sender_requeues_on_api_error(tmp_path: Path) -> None:
    collector = EventCollector(tmp_path / "q.json")
    collector.append("skill.run", payload={"slug": "a"})

    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        raise ApiError(
            status_code=500, code="DB_ERROR", message="oops", details={}
        )

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close

    sender = EventSender(collector, lambda: fake_client)
    result = await sender.send_once()
    assert result.sent == 1
    assert result.accepted == 0
    assert result.requeued == 1
    # События вернулись в очередь
    assert collector.size() == 1
    assert "DB_ERROR" in (result.last_error or "")


@pytest.mark.asyncio
async def test_sender_requeues_on_network_error(tmp_path: Path) -> None:
    collector = EventCollector(tmp_path / "q.json")
    collector.append("skill.run")

    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):  # noqa: ANN001
        raise OSError("network unreachable")

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close

    sender = EventSender(collector, lambda: fake_client)
    result = await sender.send_once()
    assert result.requeued == 1
    assert collector.size() == 1
    assert "network unreachable" in (result.last_error or "")


def test_sender_idempotency_key_is_stable() -> None:
    """Один и тот же batch → один и тот же idempotency-key (cache-friendly)."""
    batch1 = [
        {"event_type": "skill.run", "occurred_at": "2026-01-01T00:00:00+00:00", "payload": {"slug": "a"}, "metadata": {}},
        {"event_type": "skill.install", "occurred_at": "2026-01-01T00:01:00+00:00", "payload": {"slug": "b"}, "metadata": {}},
    ]
    # Permutation полей не влияет (sort_keys=True)
    batch2 = [
        {"payload": {"slug": "a"}, "occurred_at": "2026-01-01T00:00:00+00:00", "event_type": "skill.run", "metadata": {}},
        {"payload": {"slug": "b"}, "occurred_at": "2026-01-01T00:01:00+00:00", "event_type": "skill.install", "metadata": {}},
    ]
    key1 = EventSender._idempotency_key(batch1)
    key2 = EventSender._idempotency_key(batch2)
    assert key1 == key2
    assert key1.startswith("sh-cli-")
