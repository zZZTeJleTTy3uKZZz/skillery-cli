"""C3 (#1099): синк логов подключён к РЕАЛЬНЫМ путям — install и цикл демона.

Раньше на бэк уходили ровно три вызова ``report_cli_log`` (login + успешные
install/uninstall из веб-очереди), всегда ``level=info`` и только на УСПЕХ:
провалы, ради которых логи и смотрят, до веба не доезжали, а foreground-install
не синкался вовсе. Здесь закреплено обратное.

#1174: транспорт сменился — записи ложатся конвертом ``kind="log"`` в ОБЩИЙ
outbox, а доставку делает воркер (``POST /telemetry/events``). Поэтому проверяем
две вещи: (1) провал реально попадает в очередь с инициатором и причиной,
(2) доставка/её отсутствие не меняет исход install и не теряет запись.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path

import pytest
from telemetrykit import outbox

from skillery_cli import __main__ as m
from skillery_cli.core import log_sync as ls
from skillery_cli.core import outbox_worker as ow

_LOGGER_NAMES = ("skillery", "skillery.install", "skillery.reconcile", "skillery.outbox")


@pytest.fixture(autouse=True)
def _clean_loggers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


@pytest.fixture
def sync_on(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """Синк подключён — как это делает точка входа CLI/демона."""
    monkeypatch.setattr(
        "skillery_cli.core.logging_setup.log_dir", lambda: tmp_path
    )
    ls.attach_log_sync()


def queued_logs() -> list[dict]:
    return [
        e["payload"] for e in outbox.read_batch(10_000) if e["kind"] == ls.KIND_LOG
    ]


class _CapturingClient:
    """Фейковый HubClient: принимает батчи общего outbox'а и очередь устройства."""

    def __init__(self, *, queue: list[dict] | None = None) -> None:
        self.batches: list[list[dict]] = []
        self.reports: list[dict] = []
        self._queue = queue or []
        self.closed = False

    # --- /telemetry/events ---
    async def send_telemetry_batch(self, envelopes: list[dict]) -> dict:
        self.batches.append([dict(e) for e in envelopes])
        return {"accepted": [e["id"] for e in envelopes], "rejected": []}

    # --- /client-logs (исторический одиночный контракт, не трогаем) ---
    async def report_cli_logs(self, items: list[dict]) -> dict:
        return {"accepted": len(items)}

    async def report_cli_log(self, **kw) -> None:  # type: ignore[no-untyped-def]
        await self.report_cli_logs([kw])

    # --- очередь устройства ---
    async def fetch_device_queue(self, **kw) -> list[dict]:  # type: ignore[no-untyped-def]
        return list(self._queue)

    async def report_device_apply(self, **kw) -> dict:  # type: ignore[no-untyped-def]
        self.reports.append(kw)
        return {"status": "ok"}

    # --- install ---
    async def install_bundle(self, slug, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("git clone failed: repository not found")

    async def close(self) -> None:
        self.closed = True

    def delivered(self) -> list[dict]:
        return [
            e["payload"] for batch in self.batches for e in batch
            if e["kind"] == ls.KIND_LOG
        ]


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    from skillery_cli.config import ClientConfig

    c = ClientConfig.load()
    monkeypatch.setattr(
        type(c), "effective_store_dir", lambda self: tmp_path / "store"
    )
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    return c


class TestForegroundInstallSync:
    """Foreground-install (initiator=cli) теперь синкается — и успех, и провал."""

    async def test_install_failure_syncs_reason_and_initiator(
        self, cfg, sync_on, monkeypatch
    ) -> None:
        client = _CapturingClient()
        monkeypatch.setattr(m, "HubClient", lambda **kw: client)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

        with pytest.raises(RuntimeError):
            await m._install_chain(
                cfg, "tok", slug="atlas", channel="published", scope="global",
                project_path=None, force=False, agent_target=object(),
                initiator="cli",
            )

        errors = [i for i in client.delivered() if i.get("level") == "error"]
        assert errors, "провал install ОБЯЗАН уехать на бэк (раньше не уезжал)"
        ctx = errors[0]["context"]
        assert ctx["initiator"] == "cli"
        assert ctx["slug"] == "atlas"
        assert "repository not found" in ctx["error"]
        assert ctx["client_device_id"]
        assert errors[0]["ts"], "нужен ts события, а не время приёма"
        assert client.closed is True

    async def test_offline_backend_does_not_break_install(
        self, cfg, sync_on, monkeypatch
    ) -> None:
        """Сбой доставки не меняет исход install и не теряет запись."""

        class _Offline(_CapturingClient):
            async def send_telemetry_batch(self, envelopes):  # type: ignore[no-untyped-def]
                raise RuntimeError("network unreachable")

        client = _Offline()
        monkeypatch.setattr(m, "HubClient", lambda **kw: client)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

        with pytest.raises(RuntimeError, match="repository not found"):
            await m._install_chain(
                cfg, "tok", slug="atlas", channel="published", scope="global",
                project_path=None, force=False, agent_target=object(),
                initiator="cli",
            )
        assert len(queued_logs()) >= 1, "офлайн → запись ждёт следующего цикла"


class TestDaemonQueueSync:
    """Демон-путь: провал веб-задания виден, а доставка не держит цикл."""

    async def test_web_queue_failure_is_queued_with_initiator(
        self, cfg, sync_on, monkeypatch
    ) -> None:
        client = _CapturingClient(
            queue=[{"slug": "atlas", "desired_version": "0.4.0"}]
        )
        monkeypatch.setattr(m, "HubClient", lambda **kw: client)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
        monkeypatch.setattr(m, "read_meta", lambda p: {})

        async def _chain(*a, **kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk full")

        monkeypatch.setattr(m, "_install_chain", _chain)

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )
        assert rep["failed"] == ["atlas"]

        errors = [i for i in queued_logs() if i.get("level") == "error"]
        assert errors, "провал веб-задания обязан быть виден в вебе"
        ctx = errors[0]["context"]
        assert ctx["initiator"] == "web-queue"
        assert ctx["skill"] == "atlas"
        assert "disk full" in ctx["error"]

    async def test_daemon_cycle_delivers_buffered_backlog(
        self, cfg, sync_on, monkeypatch
    ) -> None:
        """Записи, накопленные офлайн, догоняют бэк проходом воркера в цикле."""
        ls.publish({
            "ts": "2026-07-24T10:00:00+00:00",
            "level": "error",
            "logger": "skillery.install",
            "message": "накоплено офлайн",
            "context": {"initiator": "daemon-auto"},
        })
        client = _CapturingClient(queue=[])

        await ow.flush_outbox(client, force=True)

        assert "накоплено офлайн" in [i["message"] for i in client.delivered()]
        assert queued_logs() == []

    async def test_daemon_cycle_is_not_blocked_by_delivery(
        self, cfg, sync_on, monkeypatch
    ) -> None:
        """Сбой доставки не валит и не держит цикл демона, запись остаётся."""

        class _Offline(_CapturingClient):
            async def send_telemetry_batch(self, envelopes):  # type: ignore[no-untyped-def]
                raise RuntimeError("502")

        ls.publish({
            "level": "error", "logger": "skillery.install",
            "message": "офлайн", "context": {},
        })
        client = _Offline(queue=[])
        monkeypatch.setattr(m, "HubClient", lambda **kw: client)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )
        assert rep == {"applied": [], "failed": [], "skipped": []}
        assert client.closed is True

        res = await ow.flush_outbox_safe(client, force=True)
        assert res.error
        assert len(queued_logs()) == 1, "офлайн НЕ теряет запись"


class TestLegacyContract:
    """Старый одиночный контракт ``report_cli_log`` не меняется (старые CLI)."""

    async def test_single_report_still_posts_old_payload(self) -> None:
        from skillery_cli.core.transport import HubClient

        sent: dict = {}

        class _Probe(HubClient):
            async def _request(self, method, path, **kw):  # type: ignore[no-untyped-def]
                sent["method"] = method
                sent["path"] = path
                sent["json"] = kw.get("json")
                return {"accepted": 1}

        client = _Probe(base_url="http://x")
        await client.report_cli_log(
            level="info", message="CLI: вход выполнен", logger="cli.login",
            context={"device": "pc"},
        )
        assert sent["method"] == "POST"
        assert sent["path"] == "/client-logs"
        # #1452: путь один (/client-logs), тип клиента — поле тела.
        assert sent["json"] == {
            "client": "cli",
            "items": [{
                "level": "info",
                "message": "CLI: вход выполнен",
                "logger": "cli.login",
                "context": {"device": "pc"},
            }]
        }
        await client.close()
