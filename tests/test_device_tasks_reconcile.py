"""#1102: демон применяет обобщённые device-задачи из очереди (push-обновления).

Ключевое: для ``cli_upgrade`` демон стартует фоновый self-upgrade до
``target_version`` и РАПОРТУЕТ факт (applied при успешном старте, failed при
провале спавна). Дедуп: если апгрейд уже идёт — не спавним второй.
``skill_update``/``generic`` — задел: лог + skip (терминальный статус не шлём).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli import __main__ as m


class _FakeClient:
    """HubClient-заглушка: полный ответ очереди + запись рапортов задач."""

    def __init__(
        self, items: list[dict] | None = None, device_tasks: list[dict] | None = None
    ) -> None:
        self._items = items or []
        self._device_tasks = device_tasks or []
        self.reports: list[dict] = []
        self.task_reports: list[dict] = []
        self.closed = False

    async def fetch_device_queue_full(self, **kw):  # type: ignore[no-untyped-def]
        self.queue_kwargs = kw
        return {"items": list(self._items), "device_tasks": list(self._device_tasks)}

    async def report_device_apply(self, **kw):  # type: ignore[no-untyped-def]
        self.reports.append(kw)
        return {"status": "applied" if kw.get("ok") else "failed"}

    async def report_device_task(self, **kw):  # type: ignore[no-untyped-def]
        self.task_reports.append(kw)
        return {"status": kw.get("status")}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    from skillery_cli.config import ClientConfig

    c = ClientConfig.load()
    monkeypatch.setattr(type(c), "effective_store_dir", lambda self: tmp_path / "store")
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    return c


def _wire(fake, monkeypatch, *, current="0.5.0", running=False):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(m, "HubClient", lambda **kw: fake)
    monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
    monkeypatch.setattr("skillery_cli.core.identity.device_uid", lambda: "dev-cdid")
    # Версию читаем из пакета (`from skillery_cli import __version__`), не из __main__.
    monkeypatch.setattr("skillery_cli.__version__", current, raising=False)
    monkeypatch.setattr(m, "_upgrade_already_running", lambda: running)


class _Agent:
    name = "claude"


class TestCliUpgradeTask:
    async def test_spawns_upgrade_to_target_and_reports_applied(
        self, cfg, monkeypatch
    ) -> None:
        fake = _FakeClient(
            device_tasks=[
                {
                    "id": 7,
                    "task_type": "cli_upgrade",
                    "payload": {"target_version": "0.6.0"},
                    "status": "queued",
                }
            ]
        )
        _wire(fake, monkeypatch, current="0.5.0")
        spawned: list[str] = []
        monkeypatch.setattr(
            m, "_spawn_background_upgrade",
            lambda *a, **k: (spawned.append(k.get("version")) or True),
        )

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )

        # апгрейд запущен ровно до target, пакет не произвольный.
        assert spawned == ["0.6.0"]
        # отрапортован applied на нужный task_id + cdid.
        assert fake.task_reports == [
            {"client_device_id": "dev-cdid", "task_id": 7, "status": "applied", "error": None}
        ]

    async def test_spawn_failure_reports_failed(self, cfg, monkeypatch) -> None:
        fake = _FakeClient(
            device_tasks=[
                {"id": 8, "task_type": "cli_upgrade", "payload": {"target_version": "0.6.0"}}
            ]
        )
        _wire(fake, monkeypatch, current="0.5.0")
        monkeypatch.setattr(m, "_spawn_background_upgrade", lambda *a, **k: False)

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )

        assert fake.task_reports[0]["task_id"] == 8
        assert fake.task_reports[0]["status"] == "failed"
        assert fake.task_reports[0]["error"]  # причина есть

    async def test_dedup_when_upgrade_already_running(self, cfg, monkeypatch) -> None:
        """Апгрейд уже идёт → НЕ спавним второй и НЕ шлём терминальный статус."""
        fake = _FakeClient(
            device_tasks=[
                {"id": 9, "task_type": "cli_upgrade", "payload": {"target_version": "0.6.0"}}
            ]
        )
        _wire(fake, monkeypatch, current="0.5.0", running=True)
        spawned: list[str] = []
        monkeypatch.setattr(
            m, "_spawn_background_upgrade",
            lambda *a, **k: (spawned.append(k.get("version")) or True),
        )

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )

        assert spawned == []  # второй апгрейд не плодим
        assert fake.task_reports == []  # задачу оставляем в очереди (backend переотдаст)

    async def test_already_at_target_reports_applied_without_spawn(
        self, cfg, monkeypatch
    ) -> None:
        """Текущая версия уже >= target → идемпотентно applied, без апгрейда (и без даунгрейда)."""
        fake = _FakeClient(
            device_tasks=[
                {"id": 10, "task_type": "cli_upgrade", "payload": {"target_version": "0.5.0"}}
            ]
        )
        _wire(fake, monkeypatch, current="0.6.0")  # уже новее target
        spawned: list[str] = []
        monkeypatch.setattr(
            m, "_spawn_background_upgrade",
            lambda *a, **k: (spawned.append(k.get("version")) or True),
        )

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )

        assert spawned == []  # не спавним и не даунгрейдим
        assert fake.task_reports[0]["status"] == "applied"

    async def test_missing_target_reports_failed(self, cfg, monkeypatch) -> None:
        fake = _FakeClient(
            device_tasks=[{"id": 11, "task_type": "cli_upgrade", "payload": {}}]
        )
        _wire(fake, monkeypatch)
        monkeypatch.setattr(m, "_spawn_background_upgrade", lambda *a, **k: True)

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )

        assert fake.task_reports[0]["status"] == "failed"


class TestNonUpgradeTasks:
    async def test_skill_update_and_generic_are_skipped_without_report(
        self, cfg, monkeypatch
    ) -> None:
        """Задел: skill_update/generic пока лог+skip, терминальный статус не шлём."""
        fake = _FakeClient(
            device_tasks=[
                {"id": 1, "task_type": "skill_update", "payload": {"slug": "atlas"}},
                {"id": 2, "task_type": "generic", "payload": {}},
            ]
        )
        _wire(fake, monkeypatch)
        spawned: list = []
        monkeypatch.setattr(
            m, "_spawn_background_upgrade", lambda *a, **k: (spawned.append(1) or True)
        )

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )

        assert spawned == []
        assert fake.task_reports == []  # не рапортуем (backend переотдаст, когда научимся)


class TestSkillQueueUntouched:
    async def test_items_still_processed_alongside_device_tasks(
        self, cfg, monkeypatch
    ) -> None:
        """device_tasks не ломают skill-очередь: items применяются как раньше."""
        fake = _FakeClient(
            items=[{"slug": "atlas", "desired_version": "0.4.0", "skill_id": 11}],
            device_tasks=[
                {"id": 7, "task_type": "cli_upgrade", "payload": {"target_version": "0.6.0"}}
            ],
        )
        _wire(fake, monkeypatch, current="0.5.0")
        monkeypatch.setattr(m, "_spawn_background_upgrade", lambda *a, **k: True)

        async def _chain(*a, **kw):  # type: ignore[no-untyped-def]
            return None

        monkeypatch.setattr(m, "_install_chain", _chain)
        monkeypatch.setattr(m, "read_meta", lambda p: {"version": "0.4.0"})

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )

        # skill применён и отрапортован по-старому.
        assert rep["applied"] == ["atlas"]
        assert fake.reports == [
            {"slug": "atlas", "ok": True, "version": "0.4.0", "skill_id": "11"}
        ]
        # device-task тоже обработан.
        assert fake.task_reports[0]["status"] == "applied"
