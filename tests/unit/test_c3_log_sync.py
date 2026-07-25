"""C3 (#1099): автосинк логов CLI на бэкенд — полнота, надёжность, троттлинг.

Владелец: логи CLI обязаны АВТОСИНХРОНИЗИРОВАТЬСЯ с хабом, чтобы разбирать
проблемы устройств из веба — с уровнями (C1), инициатором (C2) и причиной.
Раньше на бэк уходили только 3 точки и только ``level=info`` на УСПЕХ.

Здесь закреплено:
- WARNING/ERROR уходят ВСЕГДА (в т.ч. провал install и провал device-task);
- payload несёт level/logger/message/context(initiator, skill, error)/ts/
  client_device_id;
- офлайн → запись остаётся в дисковой очереди и досылается следующим циклом;
- троттлинг/батч не дают спамить бэк;
- секреты маскируются, длинные сообщения обрезаются;
- очередь ограничена по размеру (лог не съедает диск).
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

import pytest

from skillery_cli.core import log_sync as ls

_LOGGER_NAMES = ("skillery", "skillery.install", "skillery.reconcile")


@pytest.fixture(autouse=True)
def _clean_loggers():
    def _clear() -> None:
        for name in _LOGGER_NAMES:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                with suppress(Exception):
                    h.close()

    _clear()
    ls.reset_flush_throttle()
    yield
    _clear()
    ls.reset_flush_throttle()


@pytest.fixture
def queue(tmp_path: Path) -> ls.LogSyncQueue:
    return ls.LogSyncQueue(tmp_path / "sync.queue.jsonl")


class _FakeClient:
    """Клиент, принимающий батчи логов (или падающий — офлайн-режим)."""

    def __init__(self, *, offline: bool = False, hang: bool = False) -> None:
        self.batches: list[list[dict]] = []
        self._offline = offline
        self._hang = hang

    async def report_cli_logs(self, items: list[dict]) -> dict:
        if self._hang:
            await asyncio.sleep(60)
        if self._offline:
            raise RuntimeError("connection refused")
        self.batches.append([dict(i) for i in items])
        return {"accepted": len(items)}


# ═════════════════════ полнота: уровни и содержимое ═════════════════════
class TestLevelsAndPayload:
    def test_warning_and_error_are_queued_always(self, queue) -> None:
        """WARNING/ERROR из ЛЮБОГО логгера дерева skillery попадают в очередь."""
        ls.attach_log_sync(queue=queue)
        log = logging.getLogger("skillery.reconcile")

        log.warning("устройство не ответило")
        log.error("установка не удалась")

        levels = [r["level"] for r in queue.read_all()]
        assert levels == ["warning", "error"]

    def test_debug_and_access_are_not_queued_from_root(self, queue) -> None:
        """Троттлинг по уровню: трейс не гоняем по сети (иначе спам)."""
        ls.attach_log_sync(queue=queue)
        log = logging.getLogger("skillery.reconcile")
        log.setLevel(1)

        log.debug("трейс такта")
        from skillery_cli.core.logging_setup import ACCESS

        log.log(ACCESS, "опрошена очередь")

        assert queue.read_all() == []

    def test_install_audit_info_is_queued(self, queue) -> None:
        """Foreground-install синкается: install_logger пишет INFO — он уходит."""
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").info(
            "навык материализован",
            extra={"context": {"slug": "atlas", "initiator": "cli"}},
        )

        items = queue.read_all()
        assert [i["level"] for i in items] == ["info"]
        assert items[0]["context"]["initiator"] == "cli"

    def test_payload_carries_level_logger_context_ts_device(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error(
            "установка навыка не удалась",
            extra={"context": {
                "step": "install", "slug": "atlas", "initiator": "web-queue",
                "error": "disk full",
            }},
        )

        item = queue.read_all()[0]
        assert item["level"] == "error"
        assert item["logger"] == "skillery.install"
        assert item["message"] == "установка навыка не удалась"
        assert item["ts"], "без ts буферизованный лог получит время ПРИЁМА, не события"
        ctx = item["context"]
        assert ctx["initiator"] == "web-queue"
        assert ctx["slug"] == "atlas"
        assert ctx["error"] == "disk full"
        assert ctx["client_device_id"], "лог без устройства не разобрать из веба"
        assert ctx["cli_version"]
        # C1-уровень не теряется: на проводе backend-алфавит, в контексте — свой.
        assert ctx["cli_level"] == "ERROR"

    def test_cli_levels_map_to_backend_alphabet(self) -> None:
        """Бэкенд знает debug|info|warning|error — TRACE/ACCESS схлопываем вниз."""
        assert ls.wire_level("TRACE") == "debug"
        assert ls.wire_level("ACCESS") == "debug"
        assert ls.wire_level("DEBUG") == "debug"
        assert ls.wire_level("INFO") == "info"
        assert ls.wire_level("WARNING") == "warning"
        assert ls.wire_level("ERROR") == "error"
        assert ls.wire_level("CRITICAL") == "error"

    def test_exception_stack_is_captured(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        try:
            raise ValueError("бум")
        except ValueError as exc:
            logging.getLogger("skillery.daemon").error(
                "цикл упал", exc_info=exc
            )

        item = queue.read_all()[0]
        assert "ValueError" in (item.get("stack") or "")


# ═════════════════════ безопасность и лимиты записи ═════════════════════
class TestSanitizing:
    def test_secrets_are_masked(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error(
            'git clone failed: password = "hunter2supersecret"'
        )

        item = queue.read_all()[0]
        assert "hunter2supersecret" not in item["message"]

    def test_secretish_context_keys_are_dropped(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error(
            "клон не удался",
            extra={"context": {"access_token": "ghp_abcdefghijklmnop", "slug": "a"}},
        )

        ctx = queue.read_all()[0]["context"]
        assert ctx["slug"] == "a"
        assert "ghp_abcdefghijklmnop" not in json.dumps(ctx, ensure_ascii=False)

    def test_long_message_is_truncated(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error("x" * 20_000)

        assert len(queue.read_all()[0]["message"]) <= ls.MAX_MESSAGE_LEN


# ═════════════════════ надёжность: офлайн-буфер + досылка ═══════════════
class TestOfflineBuffering:
    async def test_offline_keeps_records_and_next_cycle_delivers(
        self, queue
    ) -> None:
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error("установка не удалась")

        offline = _FakeClient(offline=True)
        sent = await ls.flush_log_sync(offline, queue=queue, force=True)
        assert sent == 0
        assert queue.size() == 1, "офлайн НЕ должен терять запись"

        online = _FakeClient()
        sent = await ls.flush_log_sync(online, queue=queue, force=True)
        assert sent == 1
        assert queue.size() == 0
        assert online.batches[0][0]["message"] == "установка не удалась"

    async def test_flush_does_not_block_on_hanging_backend(self, queue) -> None:
        """Синк не имеет права держать install/демона: жёсткий таймаут."""
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error("провал")

        loop = asyncio.get_running_loop()
        started = loop.time()
        sent = await ls.flush_log_sync(
            _FakeClient(hang=True), queue=queue, force=True, timeout=0.05
        )
        assert sent == 0
        assert loop.time() - started < 5.0
        assert queue.size() == 1, "запись должна остаться на досылку"

    async def test_old_transport_without_batch_method_keeps_queue(
        self, queue
    ) -> None:
        """Старый/фейковый клиент без report_cli_logs → не теряем и не падаем."""
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error("провал")

        assert await ls.flush_log_sync(object(), queue=queue, force=True) == 0
        assert queue.size() == 1

    def test_queue_is_bounded(self, tmp_path: Path) -> None:
        q = ls.LogSyncQueue(tmp_path / "q.jsonl", max_records=5, max_bytes=2_000)
        for i in range(50):
            q.append({"level": "error", "message": f"m{i}", "context": {}})

        items = q.read_all()
        assert len(items) <= 5
        assert items[-1]["message"] == "m49", "при переполнении выкидываем СТАРЫЕ"
        assert (tmp_path / "q.jsonl").stat().st_size <= 2_000

    def test_corrupt_lines_are_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "q.jsonl"
        p.write_text('{"level":"error","message":"ок"}\nне json\n', encoding="utf-8")
        q = ls.LogSyncQueue(p)
        assert [i["message"] for i in q.read_all()] == ["ок"]


# ═════════════════════ троттлинг и батчинг ══════════════════════════════
class TestThrottling:
    async def test_flush_is_throttled_between_calls(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        log = logging.getLogger("skillery.install")

        log.error("первый")
        client = _FakeClient()
        assert await ls.flush_log_sync(client, queue=queue) == 1

        log.error("второй")
        # Сразу после успешной отправки — троттл: по сети не идём.
        assert await ls.flush_log_sync(client, queue=queue) == 0
        assert queue.size() == 1
        assert len(client.batches) == 1

        # force (цикл демона) пробивает троттл — досылаем.
        assert await ls.flush_log_sync(client, queue=queue, force=True) == 1

    async def test_batch_limit_caps_single_request(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        log = logging.getLogger("skillery.install")
        for i in range(10):
            log.error(f"провал {i}")

        client = _FakeClient()
        sent = await ls.flush_log_sync(client, queue=queue, force=True, limit=4)
        assert sent == 4
        assert len(client.batches[0]) == 4
        assert queue.size() == 6

    def test_burst_is_rate_capped(self, tmp_path: Path) -> None:
        """Спам-защита: за окно в очередь попадает не больше лимита."""
        q = ls.LogSyncQueue(tmp_path / "q.jsonl", max_records=10_000)
        ls.attach_log_sync(queue=q, rate_max=5, rate_window=60.0)
        log = logging.getLogger("skillery.install")
        for i in range(100):
            log.error(f"шторм {i}")

        items = q.read_all()
        assert len(items) <= 6, "буря должна упереться в лимит окна"
        assert any("огранич" in i["message"].lower() for i in items), (
            "факт отбрасывания обязан быть виден в логе"
        )

    def test_attach_is_idempotent(self, queue) -> None:
        ls.attach_log_sync(queue=queue)
        ls.attach_log_sync(queue=queue)
        logging.getLogger("skillery.install").error("один раз")

        assert len(queue.read_all()) == 1


# ═════════════════════ не ломаем процесс ════════════════════════════════
class TestNeverBreaksCaller:
    def test_broken_queue_does_not_raise(self, tmp_path: Path) -> None:
        """Диск недоступен → лог-синк молчит, а не валит install."""
        q = ls.LogSyncQueue(tmp_path / "q.jsonl")

        def _boom(_item):  # noqa: ANN001
            raise OSError("read-only fs")

        q.append = _boom  # type: ignore[method-assign]
        ls.attach_log_sync(queue=q)
        logging.getLogger("skillery.install").error("провал")  # не должно бросить

    async def test_flush_with_empty_queue_is_noop(self, queue) -> None:
        client = _FakeClient()
        assert await ls.flush_log_sync(client, queue=queue, force=True) == 0
        assert client.batches == []
