"""#1180: третья очередь снесена — аналитика едет через ОБЩИЙ outbox.

Что закреплено
--------------
Владелец требует ОДНУ очередь на машине. До этой задачи их было три: общий
``telemetrykit.outbox`` (навыки пишут ``kind="skill_run"``, лог-синк —
``kind="log"``) и собственная ``~/.skillery/events.queue.json`` у аналитики
(``EventCollector`` + ``EventSender``). Разъехавшиеся очереди — это разъехавшиеся
гарантии доставки, поэтому аналитика переехала в общий outbox
(``kind="analytics_event"``), а доставку ВСЕГО делает один воркер.

- продюсер (инструментация / ``event track``) пишет конверт в общий файл;
- ОТПРАВКА у аналитики своя ветка внутри того же воркера: ``POST /events``
  принимает анонимно, ``POST /telemetry/events`` требует Bearer (см.
  ``test_an_anon_flush``);
- ``EventGuard`` остался у продюсера: дедуп решает ДО публикации;
- миграция старой очереди — идемпотентно, без потерь, битые записи не роняют.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from telemetrykit import outbox

from skillery_cli.core import analytics_sync as asy
from skillery_cli.core import outbox_worker as ow


def envelopes() -> list[dict]:
    return outbox.read_batch(10_000)


class _Client:
    """Приёмник обеих веток доставки."""

    def __init__(self) -> None:
        self.events: list[list[dict]] = []
        self.batches: list[list[dict]] = []

    async def ingest_events(self, events, *, idempotency_key):  # noqa: ANN001
        self.events.append(list(events))
        self.last_idem = idempotency_key
        return {"accepted": len(events)}

    async def send_telemetry_batch(self, envs):  # noqa: ANN001
        self.batches.append(list(envs))
        return {"accepted": [e["id"] for e in envs], "rejected": []}

    async def close(self) -> None:
        return None


# ══════════════════════ продюсер пишет в ОБЩУЮ очередь ══════════════════════
class TestProducerUsesSharedQueue:
    def test_instrumentation_writes_analytics_envelope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from skillery_cli.daemon import instrumentation as instr

        monkeypatch.setattr(
            instr, "default_guard_path", lambda: tmp_path / "g.json"
        )
        instr.track_skill_event(
            "skill.install", slug="atlas", resource_id="42",
            version="1.0.0", scope="project", source="hub", agent="claude_code",
        )

        envs = envelopes()
        assert [e["kind"] for e in envs] == [asy.KIND_ANALYTICS_EVENT]
        item = envs[0]["payload"]
        assert item["event_type"] == "skill.install"
        assert item["resource_type"] == "skill"
        assert item["resource_id"] == "42"
        assert item["payload"]["slug"] == "atlas"
        assert item["metadata"] == {"source": "cli"}
        assert item["occurred_at"], "нужен момент события, а не время приёма"

    def test_third_queue_file_is_never_created(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from skillery_cli.daemon import instrumentation as instr

        monkeypatch.setattr(
            instr, "default_guard_path", lambda: tmp_path / "g.json"
        )
        instr.track_skill_event("skill.install", slug="atlas")
        assert not asy.legacy_queue_path().exists(), (
            "третья очередь запрещена: доставку делает воркер общего outbox"
        )

    def test_guard_still_dedups_before_publishing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Дедуп — свойство ПРОДЮСЕРА; переезд хранилища его не отменяет."""
        from skillery_cli.daemon import instrumentation as instr

        monkeypatch.setattr(
            instr, "default_guard_path", lambda: tmp_path / "g.json"
        )
        for _ in range(3):
            instr.track_skill_event(
                "skill.enable", slug="atlas", resource_id="42", scope="project"
            )
        assert asy.pending_count() == 1

    def test_producer_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Телеметрия не имеет права уронить команду, даже если outbox мёртв."""
        from skillery_cli.daemon import instrumentation as instr

        monkeypatch.setattr(
            instr, "default_guard_path", lambda: tmp_path / "g.json"
        )
        monkeypatch.setenv("SKILLERY_OUTBOX_DISABLED", "1")
        instr.track_skill_event("skill.install", slug="atlas")
        assert envelopes() == []


# ══════════════════════ маршрутизация по kind ═══════════════════════════════
class TestKindRouting:
    async def test_analytics_goes_to_events_not_telemetry_batch(self) -> None:
        asy.track("skill.install", resource_type="skill", resource_id="1")
        client = _Client()

        res = await ow.flush_outbox(client, force=True)

        assert client.batches == [], "аналитика НЕ едет в /telemetry/events"
        assert len(client.events) == 1
        assert client.events[0][0]["event_type"] == "skill.install"
        assert res.accepted == 1
        assert envelopes() == []

    async def test_both_kinds_ride_one_pass(self) -> None:
        outbox.append("skill_run", {"skill": "atlas"})
        asy.track("skill.enable")
        outbox.append("log", {"level": "error", "message": "провал"})
        client = _Client()

        res = await ow.flush_outbox(client, force=True)

        assert [e["kind"] for e in client.batches[0]] == ["skill_run", "log"]
        assert len(client.events[0]) == 1
        assert res.read == 3 and res.accepted == 3
        assert envelopes() == []

    async def test_idempotency_key_is_sent(self) -> None:
        asy.track("skill.install")
        client = _Client()
        await ow.flush_outbox(client, force=True)
        assert client.last_idem.startswith("sh-cli-")

    async def test_offline_keeps_analytics(self) -> None:
        asy.track("skill.install")

        class _Dead:
            async def ingest_events(self, events, *, idempotency_key):
                raise RuntimeError("connection refused")

            async def close(self) -> None:
                return None

        res = await ow.flush_outbox(_Dead(), force=True)
        assert res.removed == 0
        assert asy.pending_count() == 1, "офлайн НЕ теряет события"
        assert ow.backoff_delay() >= ow.BACKOFF_BASE_SEC

    async def test_fatal_4xx_quarantines_analytics(self) -> None:
        """422 на весь батч: повтор бессмыслен, а очередь встанет намертво."""
        asy.track("skill.install")

        class _Bad(RuntimeError):
            status_code = 422

        class _Rejecting:
            async def ingest_events(self, events, *, idempotency_key):
                raise _Bad("validation")

            async def close(self) -> None:
                return None

        res = await ow.flush_outbox(_Rejecting(), force=True)
        assert res.removed == 1
        assert asy.pending_count() == 0

    async def test_client_without_events_method_keeps_analytics(self) -> None:
        """Старый клиент без /events → ждём, а не выбрасываем."""
        asy.track("skill.install")
        res = await ow.flush_outbox(object(), force=True)
        assert res.removed == 0
        assert asy.pending_count() == 1

    async def test_envelope_without_event_type_is_dropped_not_retried(self) -> None:
        """Мусорный конверт бэкенд не примет никогда — держать его вечно нельзя."""
        outbox.append(asy.KIND_ANALYTICS_EVENT, {"payload": {"x": 1}})
        asy.track("skill.install")
        client = _Client()

        res = await ow.flush_outbox(client, force=True)

        assert len(client.events[0]) == 1, "на провод уехало только валидное"
        assert res.rejected == 1
        assert envelopes() == []

    async def test_stuck_kind_does_not_block_the_head_of_the_queue(self) -> None:
        """Окно чтения шире батча: залипший вид не морит остальные голодом."""
        for i in range(ow.BATCH_LIMIT + 5):
            outbox.append("log", {"level": "error", "message": f"n{i}"})
        asy.track("skill.install")  # позади всех логов

        class _HalfDead(_Client):
            async def send_telemetry_batch(self, envs):
                raise RuntimeError("connection refused")

        client = _HalfDead()
        await ow.flush_outbox(client, force=True)

        assert client.events, "аналитика из хвоста обязана уехать"
        assert asy.pending_count() == 0


# ══════════════════════ миграция третьей очереди ════════════════════════════
class TestLegacyAnalyticsQueueMigration:
    def _write_legacy(self, items: list[dict]) -> Path:
        p = asy.legacy_queue_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return p

    def test_legacy_records_are_moved_into_outbox(self) -> None:
        legacy = self._write_legacy([
            {"event_type": "skill.install", "occurred_at": "2026-07-24T10:00:00+00:00",
             "resource_type": "skill", "resource_id": "42",
             "payload": {"slug": "atlas"}, "metadata": {"source": "cli"}},
            {"event_type": "skill.enable", "occurred_at": "2026-07-24T10:01:00+00:00",
             "resource_type": None, "resource_id": None,
             "payload": {}, "metadata": {}},
        ])

        moved = asy.migrate_legacy_queue()

        assert moved == 2
        envs = envelopes()
        assert [e["kind"] for e in envs] == [
            asy.KIND_ANALYTICS_EVENT, asy.KIND_ANALYTICS_EVENT,
        ]
        assert [e["payload"]["event_type"] for e in envs] == [
            "skill.install", "skill.enable",
        ]
        assert envs[0]["payload"]["payload"] == {"slug": "atlas"}
        assert envs[0]["payload"]["resource_id"] == "42"
        assert "resource_id" not in envs[1]["payload"], "null не тащим на провод"
        assert not legacy.exists(), "старый файл обязан исчезнуть — очередь одна"

    def test_migration_is_idempotent(self) -> None:
        self._write_legacy([{"event_type": "skill.install", "payload": {}}])
        assert asy.migrate_legacy_queue() == 1
        assert asy.migrate_legacy_queue() == 0
        assert len(envelopes()) == 1, "повтор не должен задваивать записи"

    def test_missing_legacy_file_is_noop(self) -> None:
        assert asy.migrate_legacy_queue() == 0
        assert envelopes() == []

    def test_empty_legacy_file_is_removed(self) -> None:
        p = asy.legacy_queue_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        assert asy.migrate_legacy_queue() == 0
        assert not p.exists()

    def test_corrupt_records_are_skipped(self) -> None:
        legacy = self._write_legacy([
            {"event_type": "skill.install", "payload": {}},
            {"no_event_type": True},
            "не объект",
        ])
        assert asy.migrate_legacy_queue() == 1
        assert [e["payload"]["event_type"] for e in envelopes()] == ["skill.install"]
        assert not legacy.exists()

    def test_wholly_corrupt_file_is_set_aside_not_retried_forever(self) -> None:
        p = asy.legacy_queue_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{не json вовсе", encoding="utf-8")

        assert asy.migrate_legacy_queue() == 0
        assert not p.exists(), "иначе миграция буксовала бы на каждом старте"
        assert p.with_suffix(asy.LEGACY_CORRUPT_SUFFIX).exists(), (
            "содержимое не удаляем молча — потеря должна быть восстановимой"
        )

    def test_unavailable_outbox_keeps_the_legacy_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outbox недоступен → файл остаётся до следующего старта."""
        legacy = self._write_legacy([{"event_type": "skill.install", "payload": {}}])
        monkeypatch.setenv("SKILLERY_OUTBOX_DISABLED", "1")

        assert asy.migrate_legacy_queue() == 0
        assert legacy.exists(), "лучше повторить, чем потерять молча"

        monkeypatch.delenv("SKILLERY_OUTBOX_DISABLED")
        assert asy.migrate_legacy_queue() == 1
        assert not legacy.exists()

    def test_migration_never_raises_on_unreadable_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            asy, "legacy_queue_path", lambda: (_ for _ in ()).throw(OSError("boom"))
        )
        assert asy.migrate_legacy_queue() == 0

    def test_cli_startup_migrates_the_legacy_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Руками ничего делать не надо: первый же запуск CLI перекладывает."""
        from typer.testing import CliRunner

        import skillery_cli.__main__ as main_mod
        from skillery_cli.config import ClientConfig

        self._write_legacy([{"event_type": "skill.install",
                             "payload": {"slug": "наследство"}}])
        cfg = ClientConfig(base_url="http://localhost:8000")
        monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
        # Самолечение демона поднимает процесс — в тесте это лишнее.
        monkeypatch.setattr(main_mod, "_heal_daemon_if_dead", lambda: None)

        # ``analytics local`` — always-on и офлайн: годится как «любой запуск».
        result = CliRunner().invoke(main_mod.build_app(), ["analytics", "local"])

        assert result.exit_code == 0, result.output
        assert [e["payload"]["slug"] for e in asy.pending()] == ["наследство"]
        assert not asy.legacy_queue_path().exists()
        # Факт переноса виден и в вебе: лог-синк кладёт INFO тем же стартом.
        assert any(
            "перелита" in str(e["payload"].get("message", ""))
            for e in envelopes() if e["kind"] == "log"
        )
