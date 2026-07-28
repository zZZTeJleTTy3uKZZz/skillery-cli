"""#1174: ЕДИНАЯ очередь исходящего + воркер доставки.

Что закреплено
--------------
Владелец требует ОДНУ очередь на машину. До этой задачи их было две: общий
``telemetrykit.outbox`` (туда навыки пишут ``kind="skill_run"``) и собственная
``~/.skillery/logs/sync.queue.jsonl`` у C3-лог-синка. Разъехавшиеся очереди —
это разъехавшиеся гарантии доставки, поэтому логи переезжают в общий outbox
(``kind="log"``), а доставку ВСЕГО делает один воркер в цикле демона.

- батч уходит одним ``POST /telemetry/batch`` и подтверждается удалением;
- офлайн/5xx ⇒ конверты ОСТАЮТСЯ (ничего не теряем) + экспоненциальный backoff;
- ``rejected`` удаляются с WARNING: повтор не сделает их валидными, а очередь,
  вставшая на одном битом конверте, — это потеря ВСЕЙ остальной телеметрии;
- 4xx на весь батч (невалидное тело) ⇒ карантин батча, не бесконечный ретрай;
- троттлинг: не чаще раза в N секунд (``force`` пробивает);
- миграция старой очереди логов в outbox — идемпотентно и без потерь.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

import pytest
from telemetrykit import outbox

from skillery_cli.core import log_sync as ls
from skillery_cli.core import outbox_worker as ow

_LOGGER_NAMES = ("skillery", "skillery.install", "skillery.reconcile", "skillery.outbox")


@pytest.fixture(autouse=True)
def _isolated_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Общий outbox — в tmp; троттл/backoff воркера сброшены; логгеры чистые."""
    monkeypatch.setenv("SKILLERY_OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))
    monkeypatch.delenv("SKILLERY_OUTBOX_DISABLED", raising=False)

    def _clear() -> None:
        for name in _LOGGER_NAMES:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                with suppress(Exception):
                    h.close()

    _clear()
    ow.reset_throttle()
    yield
    _clear()
    ow.reset_throttle()


def envelopes() -> list[dict]:
    """Всё, что сейчас лежит в общем outbox."""
    return outbox.read_batch(10_000)


class _Collector(logging.Handler):
    """Ловушка записей воркера.

    Не ``caplog``: корневой логгер ``skillery`` в проде идёт с
    ``propagate=False`` (``logging_setup.configure_logging``), и соседний тест,
    вызвавший configure_logging, отрезал бы caplog от записей — ловушка вешается
    на сам логгер воркера и от порядка тестов не зависит.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def contexts(self) -> list[dict]:
        return [dict(getattr(r, "context", {}) or {}) for r in self.records]


@pytest.fixture
def worker_logs():
    """Записи логгера ``skillery.outbox`` за время теста."""
    logger = logging.getLogger(ow.LOGGER_NAME_FULL)
    collector = _Collector()
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(collector)
    try:
        yield collector
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous)


class _ApiError(RuntimeError):
    """Дублёр ``transport.ApiError``: воркер смотрит на ``status_code`` по утке."""

    def __init__(self, status_code: int, message: str = "bad") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = "API"
        self.message = message


class _FakeClient:
    """Приёмник ``POST /telemetry/batch`` с управляемым поведением."""

    def __init__(
        self,
        *,
        offline: bool = False,
        hang: bool = False,
        raises: Exception | None = None,
        reject: tuple[str, ...] = (),
    ) -> None:
        self.batches: list[list[dict]] = []
        self._offline = offline
        self._hang = hang
        self._raises = raises
        self._reject = set(reject)

    async def send_telemetry_batch(self, envs: list[dict]) -> dict:
        if self._hang:
            await asyncio.sleep(60)
        if self._raises is not None:
            raise self._raises
        if self._offline:
            raise RuntimeError("connection refused")
        self.batches.append([dict(e) for e in envs])
        accepted = [e["id"] for e in envs if e["id"] not in self._reject]
        rejected = [
            {"id": e["id"], "reason": "unknown kind"}
            for e in envs
            if e["id"] in self._reject
        ]
        return {"accepted": accepted, "rejected": rejected}

    async def close(self) -> None:
        return None


def _seed(count: int, kind: str = "skill_run") -> list[str]:
    return [
        str(outbox.append(kind, {"n": i}))
        for i in range(count)
    ]


# ══════════════════════ доставка батча и подтверждение ══════════════════════
class TestBatchDelivery:
    async def test_batch_is_sent_and_acked(self) -> None:
        _seed(3)
        client = _FakeClient()

        res = await ow.flush_outbox(client, force=True)

        assert len(client.batches) == 1
        assert len(client.batches[0]) == 3
        # Контракт конверта на проводе — дословно из ТЗ бэкенда.
        env = client.batches[0][0]
        assert set(env) == {"id", "kind", "ts", "schema_version", "payload"}
        assert res.accepted == 3
        assert res.removed == 3
        assert envelopes() == [], "accepted обязаны исчезнуть из очереди (ack)"

    async def test_batch_limit_caps_single_request(self) -> None:
        _seed(10)
        client = _FakeClient()

        res = await ow.flush_outbox(client, force=True, limit=4)

        assert res.read == 4
        assert len(client.batches[0]) == 4
        assert len(envelopes()) == 6, "хвост остаётся на следующий проход"

    async def test_empty_outbox_is_noop(self) -> None:
        client = _FakeClient()
        res = await ow.flush_outbox(client, force=True)
        assert res.read == 0
        assert client.batches == []


# ══════════════════════ офлайн: ничего не теряем ════════════════════════════
class TestOfflineNeverLoses:
    async def test_offline_keeps_envelopes_and_next_cycle_delivers(self) -> None:
        _seed(2)

        res = await ow.flush_outbox(_FakeClient(offline=True), force=True)
        assert res.removed == 0
        assert res.error
        assert len(envelopes()) == 2, "офлайн НЕ имеет права терять конверты"

        ow.reset_throttle()  # ждать backoff в тесте незачем
        online = _FakeClient()
        res = await ow.flush_outbox(online, force=True)
        assert res.accepted == 2
        assert envelopes() == []

    async def test_hanging_backend_does_not_block_caller(self) -> None:
        _seed(1)
        loop = asyncio.get_running_loop()
        started = loop.time()

        res = await ow.flush_outbox(_FakeClient(hang=True), force=True, timeout=0.05)

        assert loop.time() - started < 5.0
        assert res.removed == 0
        assert len(envelopes()) == 1

    async def test_client_without_batch_method_keeps_queue(self) -> None:
        _seed(1)
        res = await ow.flush_outbox(object(), force=True)
        assert res.removed == 0
        assert len(envelopes()) == 1, "старый клиент → ждём, а не выбрасываем"

    async def test_5xx_keeps_envelopes(self) -> None:
        _seed(1)
        res = await ow.flush_outbox(
            _FakeClient(raises=_ApiError(503, "upstream")), force=True
        )
        assert res.removed == 0
        assert len(envelopes()) == 1

    async def test_401_keeps_envelopes(self) -> None:
        """Протухший токен — не «конверт невалиден»: после refresh доедет."""
        _seed(1)
        res = await ow.flush_outbox(
            _FakeClient(raises=_ApiError(401, "expired")), force=True
        )
        assert res.removed == 0
        assert len(envelopes()) == 1

    async def test_429_keeps_envelopes(self) -> None:
        _seed(1)
        res = await ow.flush_outbox(
            _FakeClient(raises=_ApiError(429, "slow down")), force=True
        )
        assert res.removed == 0
        assert len(envelopes()) == 1


# ══════════════════════ очередь не встаёт намертво ══════════════════════════
class TestNeverStalls:
    async def test_rejected_are_removed_with_warning(self, worker_logs) -> None:
        ids = _seed(2)
        client = _FakeClient(reject=(ids[1],))

        res = await ow.flush_outbox(client, force=True)

        assert res.accepted == 1
        assert res.rejected == 1
        assert envelopes() == [], "rejected повтором валидными не станут — удаляем"
        warned = [r for r in worker_logs.records if r.levelno == logging.WARNING]
        assert any("отклон" in r.getMessage().lower() for r in warned), (
            "факт отбрасывания обязан быть виден в логе"
        )
        assert any("unknown kind" in json.dumps(c, ensure_ascii=False, default=str)
                   for c in worker_logs.contexts()), "нужна ПРИЧИНА отказа"

    async def test_invalid_batch_4xx_is_quarantined_not_retried_forever(
        self, worker_logs
    ) -> None:
        """422 на ВЕСЬ батч: повтор бессмыслен, а очередь встанет намертво."""
        _seed(2)
        client = _FakeClient(raises=_ApiError(422, "validation"))

        res = await ow.flush_outbox(client, force=True)

        assert res.removed == 2
        assert envelopes() == []
        warned = [r for r in worker_logs.records if r.levelno == logging.WARNING]
        assert warned, "карантин обязан оставить след"
        assert any(c.get("status") == 422 for c in worker_logs.contexts())

    async def test_network_failure_logs_reason(self, worker_logs) -> None:
        """Ошибка доставки — ERROR с причиной (иначе разбирать нечего)."""
        _seed(1)
        await ow.flush_outbox(_FakeClient(offline=True), force=True)

        errors = [r for r in worker_logs.records if r.levelno == logging.ERROR]
        assert errors
        ctx = dict(getattr(errors[0], "context", {}) or {})
        assert "connection refused" in str(ctx.get("error"))
        assert ctx["retry_in"] >= ow.BACKOFF_BASE_SEC

    async def test_success_is_logged_at_info_not_error(self, worker_logs) -> None:
        """«Не шуметь на стандартном уровне»: успех — INFO, а не ERROR/WARNING."""
        _seed(2)
        await ow.flush_outbox(_FakeClient(), force=True)

        levels = {r.levelno for r in worker_logs.records}
        assert logging.INFO in levels
        assert not levels & {logging.WARNING, logging.ERROR}
        ctx = worker_logs.contexts()[0]
        assert ctx["read"] == 2 and ctx["accepted"] == 2 and ctx["removed"] == 2

    async def test_broken_line_does_not_block_others(self, tmp_path: Path) -> None:
        _seed(1)
        with outbox.path().open("a", encoding="utf-8") as fh:
            fh.write("не json\n")
        outbox.append("skill_run", {"n": 99})

        client = _FakeClient()
        res = await ow.flush_outbox(client, force=True)
        assert res.accepted == 2


# ══════════════════════ троттлинг и backoff ═════════════════════════════════
class TestThrottleAndBackoff:
    async def test_throttled_between_calls(self, monkeypatch) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr("time.monotonic", lambda: clock["t"])
        _seed(1)
        client = _FakeClient()

        assert (await ow.flush_outbox(client, min_interval=30.0)).accepted == 1
        outbox.append("skill_run", {"n": 2})

        res = await ow.flush_outbox(client, min_interval=30.0)
        assert res.skipped is True
        assert len(client.batches) == 1, "троттл: по сети не идём"

        clock["t"] += 31.0
        assert (await ow.flush_outbox(client, min_interval=30.0)).accepted == 1

    async def test_force_breaks_throttle(self, monkeypatch) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr("time.monotonic", lambda: clock["t"])
        _seed(1)
        client = _FakeClient()
        await ow.flush_outbox(client, min_interval=30.0)

        outbox.append("skill_run", {"n": 2})
        assert (await ow.flush_outbox(client, min_interval=30.0, force=True)).accepted == 1

    async def test_network_failures_backoff_exponentially(self, monkeypatch) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr("time.monotonic", lambda: clock["t"])
        _seed(1)
        offline = _FakeClient(offline=True)

        await ow.flush_outbox(offline, force=True)
        first = ow.backoff_delay()
        assert first >= ow.BACKOFF_BASE_SEC

        # Даже force не должен долбить мёртвый бэкенд внутри окна backoff.
        res = await ow.flush_outbox(offline, force=True)
        assert res.skipped is True

        clock["t"] += first + 1
        await ow.flush_outbox(offline, force=True)
        assert ow.backoff_delay() > first, "backoff обязан расти"

        clock["t"] += ow.backoff_delay() + 1
        await ow.flush_outbox(_FakeClient(), force=True)
        assert ow.backoff_delay() == 0.0, "успех сбрасывает backoff"

    async def test_is_due_gates_client_construction(self, monkeypatch) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr("time.monotonic", lambda: clock["t"])
        _seed(1)
        assert ow.is_due(min_interval=30.0) is True
        await ow.flush_outbox(_FakeClient(), min_interval=30.0)
        assert ow.is_due(min_interval=30.0) is False
        clock["t"] += 31.0
        assert ow.is_due(min_interval=30.0) is True

    async def test_flush_safe_never_raises(self) -> None:
        class _Boom:
            async def send_telemetry_batch(self, envs):  # type: ignore[no-untyped-def]
                raise BaseException("catastrophe")  # noqa: TRY002

        _seed(1)
        res = await ow.flush_outbox_safe(_Boom(), force=True)
        assert res.error


# ══════════════════════ логи едут через ОБЩИЙ outbox ════════════════════════
class TestLogsUseSharedOutbox:
    def test_log_records_land_in_shared_outbox_with_kind_log(self) -> None:
        ls.attach_log_sync()
        logging.getLogger("skillery.install").error(
            "установка навыка не удалась",
            extra={"context": {"slug": "atlas", "initiator": "web-queue"}},
        )

        envs = envelopes()
        assert [e["kind"] for e in envs] == ["log"]
        payload = envs[0]["payload"]
        assert payload["level"] == "error"
        assert payload["logger"] == "skillery.install"
        assert payload["message"] == "установка навыка не удалась"
        assert payload["context"]["initiator"] == "web-queue"
        assert payload["context"]["client_device_id"]
        assert payload["ts"], "нужен ts события, а не время приёма"

    def test_no_second_queue_file_is_created(self, tmp_path: Path) -> None:
        from skillery_cli.core.logging_setup import log_dir

        ls.attach_log_sync()
        logging.getLogger("skillery.install").error("провал")

        assert not (log_dir() / ls.LEGACY_QUEUE_FILENAME).exists(), (
            "вторая очередь запрещена: доставку делает воркер общего outbox"
        )

    async def test_logs_and_skill_runs_ride_one_batch(self) -> None:
        """Одна очередь = один запрос: и телеметрия навыка, и лог CLI."""
        outbox.append("skill_run", {"skill": "atlas"})
        ls.attach_log_sync()
        logging.getLogger("skillery.install").error("провал")

        client = _FakeClient()
        await ow.flush_outbox(client, force=True)

        kinds = [e["kind"] for e in client.batches[0]]
        assert kinds == ["skill_run", "log"]

    def test_antiloop_filter_matches_worker_logger(self) -> None:
        """Фильтр петли завязан на строку — сверяем её с реальным именем логгера."""
        assert ls._WORKER_LOGGER == ow.LOGGER_NAME_FULL

    async def test_worker_own_errors_do_not_feed_the_queue(self) -> None:
        """Петля обратной связи: сбой доставки НЕ имеет права плодить конверты."""
        ls.attach_log_sync()
        _seed(1)

        await ow.flush_outbox(_FakeClient(offline=True), force=True)

        assert len(envelopes()) == 1, (
            "ошибка воркера в очередь не пишется — иначе офлайн растит её вечно"
        )


# ══════════════════════ миграция старой очереди C3 ══════════════════════════
class TestLegacyQueueMigration:
    def _legacy_path(self) -> Path:
        from skillery_cli.core.logging_setup import log_dir

        return log_dir() / ls.LEGACY_QUEUE_FILENAME

    def _write_legacy(self, items: list[dict]) -> Path:
        p = self._legacy_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n",
            encoding="utf-8",
        )
        return p

    def test_legacy_records_are_moved_into_outbox(self) -> None:
        legacy = self._write_legacy([
            {"ts": "2026-07-24T10:00:00+00:00", "level": "error",
             "logger": "skillery.install", "message": "накоплено офлайн",
             "context": {"initiator": "daemon-auto"}},
            {"ts": None, "level": "warning", "logger": "skillery",
             "message": "второе", "context": {}},
        ])

        moved = ls.migrate_legacy_queue()

        assert moved == 2
        envs = envelopes()
        assert [e["kind"] for e in envs] == ["log", "log"]
        assert [e["payload"]["message"] for e in envs] == ["накоплено офлайн", "второе"]
        assert not legacy.exists(), "старый файл обязан исчезнуть — очередь одна"

    def test_migration_is_idempotent(self) -> None:
        self._write_legacy([{"level": "error", "message": "раз", "context": {}}])
        assert ls.migrate_legacy_queue() == 1
        assert ls.migrate_legacy_queue() == 0
        assert len(envelopes()) == 1, "повтор не должен задваивать записи"

    def test_missing_legacy_file_is_noop(self) -> None:
        assert ls.migrate_legacy_queue() == 0
        assert envelopes() == []

    def test_corrupt_legacy_lines_are_skipped(self) -> None:
        p = self._legacy_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"level":"error","message":"ок","context":{}}\nне json\n',
            encoding="utf-8",
        )
        assert ls.migrate_legacy_queue() == 1
        assert [e["payload"]["message"] for e in envelopes()] == ["ок"]

    def test_attach_migrates_on_startup(self) -> None:
        """Старт CLI/демона сам перекладывает наследство — руками ничего не надо."""
        self._write_legacy([{"level": "error", "message": "наследство", "context": {}}])

        ls.attach_log_sync()

        messages = [e["payload"].get("message") for e in envelopes()]
        assert "наследство" in messages
        assert not self._legacy_path().exists()


# ══════════════════════ контракт транспорта ═════════════════════════════════
class TestTransportContract:
    async def test_post_telemetry_batch_body(self) -> None:
        from skillery_cli.core.transport import HubClient

        sent: dict = {}

        class _Probe(HubClient):
            async def _request(self, method, path, **kw):  # type: ignore[no-untyped-def]
                sent["method"] = method
                sent["path"] = path
                sent["json"] = kw.get("json")
                return {"accepted": ["a"], "rejected": [{"id": "b", "reason": "x"}]}

        client = _Probe(base_url="http://x")
        envs = [{"id": "a", "kind": "log", "ts": "T", "schema_version": 1,
                 "payload": {"level": "error"}}]
        resp = await client.send_telemetry_batch(envs)

        assert sent["method"] == "POST"
        assert sent["path"] == "/telemetry/batch"
        assert sent["json"] == {"envelopes": envs}
        assert resp["accepted"] == ["a"]
        await client.close()

    async def test_empty_response_is_dict(self) -> None:
        from skillery_cli.core.transport import HubClient

        class _Probe(HubClient):
            async def _request(self, method, path, **kw):  # type: ignore[no-untyped-def]
                return None

        client = _Probe(base_url="http://x")
        assert await client.send_telemetry_batch([]) == {}
        await client.close()


# ══════════════════════ воркер встроен в цикл демона ════════════════════════
async def test_daemon_loop_flushes_outbox_with_throttle(monkeypatch) -> None:
    """Проход доставки живёт в цикле демона и троттлится (а не бьёт каждый такт)."""
    import skillery_cli.commands.daemon as daemon_mod
    import skillery_cli.config as config_mod
    import skillery_cli.core.agents as agents_mod
    from skillery_cli import __main__ as m
    from skillery_cli.commands import _common
    from skillery_cli.config import ClientConfig

    cfg = ClientConfig(base_url="http://localhost:8000")
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]  # is_logged_in() ⇒ reconcile подключён
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(config_mod, "load_tokens", lambda email: ("tok", "rt"))
    monkeypatch.setattr(agents_mod, "get_target", lambda name: object())

    async def _noop(*a, **k):
        return {}

    monkeypatch.setattr(m, "_reconcile_device_queue", _noop)
    monkeypatch.setattr(m, "_reconcile_hub_installs", _noop)
    monkeypatch.setattr(m, "_auto_update_hub_installs", _noop)
    monkeypatch.setattr(m, "_daemon_cli_self_upgrade", _noop)

    client = _FakeClient()
    monkeypatch.setattr(_common, "make_client", lambda cfg, access: client)

    clock = {"t": 5000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    runner = daemon_mod._build_runner(interval_seconds=60)
    assert runner._reconcile is not None

    outbox.append("skill_run", {"n": 1})
    await runner._reconcile()
    assert len(client.batches) == 1, "воркер обязан ехать в цикле демона"
    assert envelopes() == []

    outbox.append("skill_run", {"n": 2})
    await runner._reconcile()
    assert len(client.batches) == 1, "второй такт подряд — троттл, сети нет"

    clock["t"] += daemon_mod._OUTBOX_FLUSH_MIN_INTERVAL_SEC + 1
    await runner._reconcile()
    assert len(client.batches) == 2
    assert envelopes() == []
