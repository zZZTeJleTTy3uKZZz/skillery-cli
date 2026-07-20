"""#912: автообновление рапортует факт, иначе веб не узнает о новой версии.

По модели #906 «установлено vX» рождается ТОЛЬКО из рапорта устройства.
Значит автообновление, которое молча подняло навык, оставило бы веб со старой
версией — ровно та жалоба, с которой начинался эпик («в профиле 0.2.2»).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli import __main__ as m


class _FakeClient:
    def __init__(self) -> None:
        self.reports: list[dict] = []
        self.closed = False

    async def report_device_apply(self, **kw):  # type: ignore[no-untyped-def]
        self.reports.append(kw)
        return {"status": "applied"}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    from skillery_cli.config import ClientConfig

    c = ClientConfig.load()
    monkeypatch.setattr(type(c), "effective_store_dir", lambda self: tmp_path / "store")
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    return c


class TestReportAutoUpdates:
    async def test_reports_version_from_store(self, cfg, monkeypatch) -> None:
        fake = _FakeClient()
        monkeypatch.setattr(m, "HubClient", lambda **kw: fake)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
        monkeypatch.setattr(m, "read_meta", lambda p: {"version": "0.3.0"})

        await m._report_auto_updates(cfg, "tok", ["atlas"])

        assert fake.reports == [{"slug": "atlas", "ok": True, "version": "0.3.0"}]
        assert fake.closed is True

    async def test_skips_skill_without_version_in_store(self, cfg, monkeypatch) -> None:
        """Нет версии в сторе — рапортовать нечего (лучше молчать, чем врать)."""
        fake = _FakeClient()
        monkeypatch.setattr(m, "HubClient", lambda **kw: fake)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
        monkeypatch.setattr(m, "read_meta", lambda p: {})

        await m._report_auto_updates(cfg, "tok", ["atlas"])
        assert fake.reports == []

    async def test_old_backend_does_not_break_background_pass(
        self, cfg, monkeypatch
    ) -> None:
        class _Old(_FakeClient):
            async def report_device_apply(self, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("404")

        fake = _Old()
        monkeypatch.setattr(m, "HubClient", lambda **kw: fake)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
        monkeypatch.setattr(m, "read_meta", lambda p: {"version": "0.3.0"})

        await m._report_auto_updates(cfg, "tok", ["atlas"])  # не падает
        assert fake.closed is True
