"""C3 (#1099): синк логов подключён к РЕАЛЬНЫМ путям — install и цикл демона.

Раньше на бэк уходили ровно три вызова ``report_cli_log`` (login + успешные
install/uninstall из веб-очереди), всегда ``level=info`` и только на УСПЕХ:
провалы, ради которых логи и смотрят, до веба не доезжали, а foreground-install
не синкался вовсе. Здесь закреплено обратное.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path

import pytest

from skillery_cli import __main__ as m
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
def sync_queue(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """Буфер синка в tmp + подключённый handler (как делает точка входа CLI)."""
    monkeypatch.setattr(
        "skillery_cli.core.logging_setup.log_dir", lambda: tmp_path
    )
    q = ls.LogSyncQueue(tmp_path / "sync.queue.jsonl")
    ls.attach_log_sync(queue=q)
    return q


class _LogCapturingClient:
    """Фейковый HubClient, принимающий батчи /cli-logs."""

    def __init__(self, *, queue: list[dict] | None = None) -> None:
        self.log_batches: list[list[dict]] = []
        self.reports: list[dict] = []
        self._queue = queue or []
        self.closed = False

    # --- /cli-logs ---
    async def report_cli_logs(self, items: list[dict]) -> dict:
        self.log_batches.append([dict(i) for i in items])
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

    def synced_items(self) -> list[dict]:
        return [item for batch in self.log_batches for item in batch]


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
        self, cfg, sync_queue, monkeypatch
    ) -> None:
        client = _LogCapturingClient()
        monkeypatch.setattr(m, "HubClient", lambda **kw: client)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

        with pytest.raises(RuntimeError):
            await m._install_chain(
                cfg, "tok", slug="atlas", channel="published", scope="global",
                project_path=None, force=False, agent_target=object(),
                initiator="cli",
            )

        items = client.synced_items()
        errors = [i for i in items if i.get("level") == "error"]
        assert errors, "провал install ОБЯЗАН уехать на бэк (раньше не уезжал)"
        ctx = errors[0]["context"]
        assert ctx["initiator"] == "cli"
        assert ctx["slug"] == "atlas"
        assert "repository not found" in ctx["error"]
        assert ctx["client_device_id"]
        assert errors[0]["ts"], "нужен ts события, а не время приёма"
        assert client.closed is True

    async def test_offline_backend_does_not_break_install(
        self, cfg, sync_queue, monkeypatch
    ) -> None:
        """Сбой синка не меняет исход install и не теряет запись."""

        class _Offline(_LogCapturingClient):
            async def report_cli_logs(self, items):  # type: ignore[no-untyped-def]
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
        assert sync_queue.size() >= 1, "офлайн → запись ждёт следующего цикла"


class TestDaemonQueueSync:
    """Демон-путь: провал веб-задания и досылка буфера каждым циклом."""

    async def test_web_queue_failure_syncs_with_initiator(
        self, cfg, sync_queue, monkeypatch
    ) -> None:
        client = _LogCapturingClient(
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

        errors = [i for i in client.synced_items() if i.get("level") == "error"]
        assert errors, "провал веб-задания обязан быть виден в вебе"
        ctx = errors[0]["context"]
        assert ctx["initiator"] == "web-queue"
        assert ctx["skill"] == "atlas"
        assert "disk full" in ctx["error"]

    async def test_cycle_delivers_buffered_backlog(
        self, cfg, sync_queue, monkeypatch
    ) -> None:
        """Записи, накопленные офлайн, догоняют бэк следующим циклом демона."""
        sync_queue.append({
            "ts": "2026-07-24T10:00:00+00:00",
            "level": "error",
            "logger": "skillery.install",
            "message": "накоплено офлайн",
            "context": {"initiator": "daemon-auto"},
        })
        client = _LogCapturingClient(queue=[])
        monkeypatch.setattr(m, "HubClient", lambda **kw: client)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )

        messages = [i["message"] for i in client.synced_items()]
        assert "накоплено офлайн" in messages
        assert sync_queue.size() == 0

    async def test_daemon_cycle_is_not_blocked_by_log_sync(
        self, cfg, sync_queue, monkeypatch
    ) -> None:
        """Синк не валит и не держит цикл демона, если бэк отвечает ошибкой."""

        class _Offline(_LogCapturingClient):
            async def report_cli_logs(self, items):  # type: ignore[no-untyped-def]
                raise RuntimeError("502")

        sync_queue.append({
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
        assert sync_queue.size() == 1
        assert client.closed is True


class TestLegacyContract:
    """Старый одиночный контракт ``report_cli_log`` не меняется."""

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
        assert sent["path"] == "/cli-logs"
        assert sent["json"] == {
            "items": [{
                "level": "info",
                "message": "CLI: вход выполнен",
                "logger": "cli.login",
                "context": {"device": "pc"},
            }]
        }
        await client.close()
