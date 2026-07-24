"""#905: демон забирает очередь устройства, применяет и РАПОРТУЕТ факт.

Ключевое: успех рапортуется версией, которая РЕАЛЬНО легла в стор (а не той,
что просили), а провал рапортуется как провал — иначе веб снова показывал бы
намерение вместо факта.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli import __main__ as m


class _FakeClient:
    def __init__(self, queue: list[dict]) -> None:
        self._queue = queue
        self.reports: list[dict] = []
        self.closed = False

    async def fetch_device_queue(self, **kw) -> list[dict]:  # type: ignore[no-untyped-def]
        # #919: демон шлёт сюда состояние автообновления машины.
        self.queue_kwargs = kw
        return list(self._queue)

    async def report_device_apply(self, **kw) -> dict:  # type: ignore[no-untyped-def]
        self.reports.append(kw)
        return {"status": "applied" if kw.get("ok") else "failed"}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    from skillery_cli.config import ClientConfig

    c = ClientConfig.load()
    monkeypatch.setattr(
        type(c), "effective_store_dir", lambda self: tmp_path / "store"
    )
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    return c


def _install(fake_client, monkeypatch, *, fail: bool = False):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(m, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

    async def _chain(*a, **kw):  # type: ignore[no-untyped-def]
        if fail:
            raise RuntimeError("disk full")

    monkeypatch.setattr(m, "_install_chain", _chain)


class TestReconcileDeviceQueue:
    async def test_applies_and_reports_real_version(
        self, cfg, tmp_path: Path, monkeypatch
    ) -> None:
        """Рапортуем версию из стора — ФАКТ, а не то, что просили."""
        fake = _FakeClient([{"slug": "atlas", "desired_version": "0.4.0", "skill_id": 11}])
        _install(fake, monkeypatch)
        # На диск реально легла 0.4.0 (мету пишет installer).
        monkeypatch.setattr(m, "read_meta", lambda p: {"version": "0.4.0"})

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )
        assert rep["applied"] == ["atlas"]
        assert fake.reports == [
            {"slug": "atlas", "ok": True, "version": "0.4.0"}
        ]
        assert fake.closed is True

    async def test_failure_reported_as_failure(
        self, cfg, monkeypatch
    ) -> None:
        fake = _FakeClient([{"slug": "atlas", "desired_version": "0.4.0"}])
        _install(fake, monkeypatch, fail=True)
        monkeypatch.setattr(m, "read_meta", lambda p: {})

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )
        assert rep["failed"] == ["atlas"]
        assert fake.reports[0]["ok"] is False
        assert "disk full" in fake.reports[0]["error"]

    async def test_empty_queue_is_noop(self, cfg, monkeypatch) -> None:
        fake = _FakeClient([])
        _install(fake, monkeypatch)
        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )
        assert rep == {"applied": [], "failed": [], "skipped": []}
        assert fake.reports == []

    async def test_old_backend_without_queue_degrades_silently(
        self, cfg, monkeypatch
    ) -> None:
        """Нет эндпоинта/устройства → уступаем legacy-пути, не падаем."""

        class _Old(_FakeClient):
            async def fetch_device_queue(self, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("404")

        fake = _Old([])
        _install(fake, monkeypatch)
        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )
        assert rep["applied"] == [] and rep["failed"] == []


class _Agent:
    name = "claude"


class TestReconcileRemoval:
    """#13: removal-задание (action=remove) СНИМАЕТ навык, а не ставит."""

    def _mock_uninstall(self, monkeypatch):  # type: ignore[no-untyped-def]
        removed: dict = {}

        class _FakeInstaller:
            def __init__(self, target, store):  # type: ignore[no-untyped-def]
                pass

            def remove(self, **kw):  # type: ignore[no-untyped-def]
                if getattr(_FakeInstaller, "_boom", False):
                    raise RuntimeError("permission denied")
                removed.update(kw)

        monkeypatch.setattr(m, "SkillInstaller", _FakeInstaller)
        monkeypatch.setattr(m, "_revert_tooling", lambda *a, **k: None)
        monkeypatch.setattr(m, "track_skill_event", lambda *a, **k: None)
        return removed, _FakeInstaller

    async def test_removal_uninstalls_and_reports_ok(
        self, cfg, monkeypatch
    ) -> None:
        fake = _FakeClient(
            [{"slug": "atlas", "desired_version": "0.4.0", "action": "remove"}]
        )
        _install(fake, monkeypatch)
        removed, _ = self._mock_uninstall(monkeypatch)

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )
        assert rep["applied"] == ["atlas"]
        # снос из стора + global-ссылки (purge=True, project=None)
        assert removed == {
            "slug": "atlas",
            "project": None,
            "keep_local": False,
            "purge": True,
        }
        # рапорт об успешном снятии — БЕЗ версии.
        assert fake.reports == [{"slug": "atlas", "ok": True}]

    async def test_removal_failure_reported(self, cfg, monkeypatch) -> None:
        fake = _FakeClient(
            [{"slug": "atlas", "desired_version": "0.4.0", "action": "remove"}]
        )
        _install(fake, monkeypatch)
        _, Installer = self._mock_uninstall(monkeypatch)
        Installer._boom = True  # type: ignore[attr-defined]

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )
        assert rep["failed"] == ["atlas"]
        assert fake.reports[0]["ok"] is False
        assert "permission denied" in fake.reports[0]["error"]


class TestAutoUpdateStateReported:
    """#919: демон сообщает хабу, включено ли автообновление на этой машине."""

    async def test_queue_poll_carries_auto_update_flag(
        self, cfg, monkeypatch
    ) -> None:
        fake = _FakeClient([])
        _install(fake, monkeypatch)
        cfg.auto_update = False

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=object()
        )

        assert fake.queue_kwargs == {
            "auto_update": False,
            "wait": 0,
            "supports_removal": True,
        }, "веб не узнает про выключенное автообновление на устройстве"
