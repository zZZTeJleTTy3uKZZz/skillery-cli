"""C3 (#1099): автосинк логов CLI — полнота записи, безопасность, анти-спам.

Владелец: логи CLI обязаны АВТОСИНХРОНИЗИРОВАТЬСЯ с хабом, чтобы разбирать
проблемы устройств из веба — с уровнями (C1), инициатором (C2) и причиной.
Раньше на бэк уходили только 3 точки и только ``level=info`` на УСПЕХ.

#1174: запись идёт в ОБЩИЙ outbox (``telemetrykit.outbox``, ``kind="log"``) —
своей очереди у синка больше нет, доставку делает один воркер
(:mod:`skillery_cli.core.outbox_worker`, тесты — ``test_outbox_worker.py``).

Здесь закреплено:
- WARNING/ERROR уходят ВСЕГДА (в т.ч. провал install и провал device-task);
- payload несёт level/logger/message/context(initiator, skill, error)/ts/
  client_device_id — та же форма, что и раньше на ``POST /cli-logs``;
- секреты маскируются, длинные сообщения обрезаются;
- шторм ошибок упирается в rate-limit, и факт отбрасывания виден.
"""
from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path

import pytest
from telemetrykit import outbox

from skillery_cli.core import log_sync as ls

_LOGGER_NAMES = ("skillery", "skillery.install", "skillery.reconcile")


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    yield
    _clear()


def items() -> list[dict]:
    """Payload'ы конвертов ``kind="log"`` из общего outbox'а."""
    return [e["payload"] for e in outbox.read_batch(10_000) if e["kind"] == ls.KIND_LOG]


# ═════════════════════ полнота: уровни и содержимое ═════════════════════
class TestLevelsAndPayload:
    def test_warning_and_error_are_queued_always(self) -> None:
        """WARNING/ERROR из ЛЮБОГО логгера дерева skillery попадают в очередь."""
        ls.attach_log_sync()
        log = logging.getLogger("skillery.reconcile")

        log.warning("устройство не ответило")
        log.error("установка не удалась")

        assert [i["level"] for i in items()] == ["warning", "error"]

    def test_debug_and_access_are_not_queued_from_root(self) -> None:
        """Троттлинг по уровню: трейс не гоняем по сети (иначе спам)."""
        ls.attach_log_sync()
        log = logging.getLogger("skillery.reconcile")
        log.setLevel(1)

        log.debug("трейс такта")
        from skillery_cli.core.logging_setup import ACCESS

        log.log(ACCESS, "опрошена очередь")

        assert items() == []

    def test_install_audit_info_is_queued(self) -> None:
        """Foreground-install синкается: install_logger пишет INFO — он уходит."""
        ls.attach_log_sync()
        logging.getLogger("skillery.install").info(
            "навык материализован",
            extra={"context": {"slug": "atlas", "initiator": "cli"}},
        )

        got = items()
        assert [i["level"] for i in got] == ["info"]
        assert got[0]["context"]["initiator"] == "cli"

    def test_payload_carries_level_logger_context_ts_device(self) -> None:
        ls.attach_log_sync()
        logging.getLogger("skillery.install").error(
            "установка навыка не удалась",
            extra={"context": {
                "step": "install", "slug": "atlas", "initiator": "web-queue",
                "error": "disk full",
            }},
        )

        item = items()[0]
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

    def test_exception_stack_is_captured(self) -> None:
        ls.attach_log_sync()
        try:
            raise ValueError("бум")
        except ValueError as exc:
            logging.getLogger("skillery.daemon").error("цикл упал", exc_info=exc)

        assert "ValueError" in (items()[0].get("stack") or "")


# ═════════════════════ безопасность и лимиты записи ═════════════════════
class TestSanitizing:
    def test_secrets_are_masked(self) -> None:
        ls.attach_log_sync()
        logging.getLogger("skillery.install").error(
            'git clone failed: password = "hunter2supersecret"'
        )

        assert "hunter2supersecret" not in items()[0]["message"]

    def test_secretish_context_keys_are_dropped(self) -> None:
        ls.attach_log_sync()
        logging.getLogger("skillery.install").error(
            "клон не удался",
            extra={"context": {"access_token": "ghp_abcdefghijklmnop", "slug": "a"}},
        )

        ctx = items()[0]["context"]
        assert ctx["slug"] == "a"
        assert "ghp_abcdefghijklmnop" not in json.dumps(ctx, ensure_ascii=False)

    def test_long_message_is_truncated(self) -> None:
        ls.attach_log_sync()
        logging.getLogger("skillery.install").error("x" * 20_000)

        assert len(items()[0]["message"]) <= ls.MAX_MESSAGE_LEN


# ═════════════════════ анти-спам на запись ══════════════════════════════
class TestRateLimit:
    def test_burst_is_rate_capped(self) -> None:
        """Спам-защита: за окно в очередь попадает не больше лимита."""
        ls.attach_log_sync(rate_max=5, rate_window=60.0)
        log = logging.getLogger("skillery.install")
        for i in range(100):
            log.error(f"шторм {i}")

        got = items()
        assert len(got) <= 6, "буря должна упереться в лимит окна"
        assert any("огранич" in i["message"].lower() for i in got), (
            "факт отбрасывания обязан быть виден в логе"
        )

    def test_attach_is_idempotent(self) -> None:
        ls.attach_log_sync()
        ls.attach_log_sync()
        logging.getLogger("skillery.install").error("один раз")

        assert len(items()) == 1


# ═════════════════════ не ломаем процесс ════════════════════════════════
class TestNeverBreaksCaller:
    def test_broken_sink_does_not_raise(self) -> None:
        """Диск недоступен → лог-синк молчит, а не валит install."""

        def _boom(_item):  # noqa: ANN001
            raise OSError("read-only fs")

        ls.attach_log_sync(sink=_boom)
        logging.getLogger("skillery.install").error("провал")  # не должно бросить

    def test_publish_never_raises_when_outbox_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("SKILLERY_OUTBOX_DISABLED", "1")
        assert ls.publish({"level": "error", "message": "x"}) is None
