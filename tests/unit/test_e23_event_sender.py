"""``OutboxSender`` — такт демона поверх ОБЩЕЙ исходящей очереди (#1180).

Раньше здесь жил ``EventSender``, снимавший батч со СВОЕЙ очереди
(``events.queue.json``). Очередь снесена, но контракт такта («один вызов →
``SendResult`` с sent/accepted/requeued») сохранён дословно: на нём стоит
backoff-классификация ``DaemonRunner``'а и накопительный ``daemon status``.

Что закрепляем:
- успех → ``accepted``, конверты исчезли из очереди;
- офлайн/5xx → ``requeued == sent``, НИЧЕГО не потеряно (это и есть «провал»
  для backoff'а рунера);
- пустая очередь и троттл → пустой результат, а НЕ провал (иначе демон
  экспоненциально засыпал бы на ровном месте);
- ключ идемпотентности стабилен по содержимому батча.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from telemetrykit import outbox

from skillery_cli.core import analytics_sync
from skillery_cli.core import outbox_worker as ow
from skillery_cli.daemon.event_sender import OutboxSender


class _Client:
    """Приёмник обеих веток: ``/events`` и ``/telemetry/events``."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.events: list[list[dict]] = []
        self.batches: list[list[dict]] = []
        self._fail = fail

    async def ingest_events(self, events, *, idempotency_key):  # noqa: ANN001
        if self._fail is not None:
            raise self._fail
        self.events.append(list(events))
        return {"accepted": len(events)}

    async def send_telemetry_batch(self, envelopes):  # noqa: ANN001
        if self._fail is not None:
            raise self._fail
        self.batches.append(list(envelopes))
        return {"accepted": [e["id"] for e in envelopes], "rejected": []}

    async def close(self) -> None:
        return None


async def test_send_once_empty_queue_is_noop(tmp_path: Path) -> None:
    client = _Client()
    result = await OutboxSender(lambda: client).send_once(force=True)
    assert result.sent == 0
    assert result.accepted == 0
    assert result.requeued == 0
    assert client.events == [] and client.batches == []


async def test_send_once_delivers_and_acks(tmp_path: Path) -> None:
    analytics_sync.track("skill.install", resource_type="skill", resource_id="1")
    client = _Client()

    result = await OutboxSender(lambda: client).send_once(force=True)

    assert result.sent == 1
    assert result.accepted == 1
    assert result.requeued == 0
    assert analytics_sync.pending_count() == 0


async def test_send_once_requeues_on_network_failure(tmp_path: Path) -> None:
    """Офлайн = «слали, но всё вернулось» — ровно то, что рунер зовёт провалом."""
    analytics_sync.track("skill.install", resource_type="skill", resource_id="1")
    client = _Client(fail=RuntimeError("connection refused"))

    result = await OutboxSender(lambda: client).send_once(force=True)

    assert result.sent == 1
    assert result.accepted == 0
    assert result.requeued == 1, "requeued >= sent ⇒ рунер вырастит backoff"
    assert result.last_error
    assert analytics_sync.pending_count() == 1, "офлайн не теряет события"


async def test_send_once_carries_every_kind_in_one_pass(tmp_path: Path) -> None:
    """Очередь одна: за такт уезжают и запуски навыков, и логи, и аналитика."""
    outbox.append("skill_run", {"skill": "atlas"})
    outbox.append("log", {"level": "error", "message": "провал"})
    analytics_sync.track("skill.enable", resource_type="skill", resource_id="7")
    client = _Client()

    result = await OutboxSender(lambda: client).send_once(force=True)

    assert result.sent == 3
    assert result.accepted == 3
    assert [e["kind"] for e in client.batches[0]] == ["skill_run", "log"]
    assert client.events[0][0]["event_type"] == "skill.enable"
    assert outbox.read_batch(100) == []


async def test_send_once_respects_worker_throttle(tmp_path: Path) -> None:
    """Такт демона зовётся раз в ~2с; по сети идём НЕ каждый раз."""
    analytics_sync.track("skill.install")
    client = _Client()
    sender = OutboxSender(lambda: client, min_interval=30.0)

    first = await sender.send_once()
    assert first.accepted == 1

    analytics_sync.track("skill.install", payload={"n": 2})
    second = await sender.send_once()
    assert second.sent == 0, "троттл: по сети не идём"
    assert second.requeued == 0, "троттл — НЕ провал, backoff расти не должен"
    assert analytics_sync.pending_count() == 1


async def test_send_once_without_client_is_noop(tmp_path: Path) -> None:
    """Фабрика не дала клиента (нет конфига/сети) → просто не в этот раз."""
    analytics_sync.track("skill.install")

    def _factory(anonymous: bool = False):
        return None

    result = await OutboxSender(_factory).send_once(force=True)
    assert result == type(result)(sent=0, accepted=0, skipped=0, requeued=0)
    assert analytics_sync.pending_count() == 1


async def test_send_once_never_raises(tmp_path: Path) -> None:
    """Такт демона не имеет права упасть из-за отправки."""
    analytics_sync.track("skill.install")

    def _factory(anonymous: bool = False):
        raise RuntimeError("фабрика взорвалась")

    result = await OutboxSender(_factory).send_once(force=True)
    assert result.sent == 0


def test_idempotency_key_is_stable_by_content() -> None:
    batch1 = [{"event_type": "skill.run", "occurred_at": "2026-01-01T00:00:00Z"}]
    batch2 = [{"occurred_at": "2026-01-01T00:00:00Z", "event_type": "skill.run"}]
    assert analytics_sync.idempotency_key(batch1) == (
        analytics_sync.idempotency_key(batch2)
    ), "порядок ключей не должен менять Idempotency-Key"
    other = [{"event_type": "skill.install", "occurred_at": "2026-01-01T00:00:00Z"}]
    assert analytics_sync.idempotency_key(batch1) != (
        analytics_sync.idempotency_key(other)
    )


async def test_flush_result_maps_to_send_result_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отображение исхода воркера в SendResult — контракт для рунера/статуса."""
    analytics_sync.track("skill.install")

    async def _fake_flush(client, **kw):  # noqa: ANN001
        return ow.OutboxFlushResult(read=10, accepted=6, rejected=1, removed=7)

    monkeypatch.setattr(ow, "flush_outbox_safe", _fake_flush)
    result = await OutboxSender(lambda: _Client()).send_once(force=True)

    assert result.sent == 10
    assert result.accepted == 6
    assert result.skipped == 3  # прочитано, но ни принято, ни отклонено
    assert result.requeued == 3  # осталось в очереди
