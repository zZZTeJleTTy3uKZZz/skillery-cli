"""Видимость device-задач в ``daemon.log`` + регресс подмены access-токена.

Требование владельца: «видеть КАЖДУЮ задачу», а не только провалы. Успех и
пропуск device-задач писались уровнем ACCESS(15), стандартный уровень демона —
ERROR, поэтому в ``daemon.log`` их не было вовсе: задача «применилась», а следа
нет. Здесь они переведены на ``install_logger`` (свой INFO-хендлер, пишет
всегда); ACCESS остаётся для шума long-poll-опросов.

Второй (и более злой) регресс, зафиксированный тут же: в ``_reconcile_device_queue``
локальный ``from ... import access`` ПЕРЕКРЫВАЛ одноимённый параметр-токен, и в
``HubClient(access_token=…)`` / ``_install_chain`` уходил объект функции. Наружу
это выглядело как «очередь всегда пуста» — device_tasks не применялись никогда.
"""
from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path

import pytest

from skillery_cli import __main__ as m
from skillery_cli.core import logging_setup as ls

_LOGGER_NAMES = ("skillery", "skillery.reconcile", "skillery.install")


@pytest.fixture(autouse=True)
def _reset_loggers():
    def _clear() -> None:
        for name in _LOGGER_NAMES:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                with suppress(Exception):
                    h.close()

    _clear()
    ls._configured = False
    yield
    _clear()
    ls._configured = False


@pytest.fixture
def daemon_log(tmp_path: Path, monkeypatch) -> Path:
    """Уровень демона — БОЕВОЙ (ERROR): именно на нём задачи и пропадали."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("error", filename="daemon.log")
    return tmp_path / "daemon.log"


def _records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


class _FakeClient:
    def __init__(self, device_tasks: list[dict] | None = None) -> None:
        self._device_tasks = device_tasks or []
        self.task_reports: list[dict] = []
        self.init_kwargs: dict = {}
        self.closed = False

    async def fetch_device_queue_full(self, **kw):  # type: ignore[no-untyped-def]
        return {"items": [], "device_tasks": list(self._device_tasks)}

    async def report_device_task(self, **kw):  # type: ignore[no-untyped-def]
        self.task_reports.append(kw)
        return {"status": kw.get("status")}

    async def close(self) -> None:
        self.closed = True


def _task(target: str = "0.9.9", task_id: int = 7, ttype: str = "cli_upgrade") -> dict:
    return {
        "id": task_id,
        "task_type": ttype,
        "payload": {"target_version": target},
        "status": "queued",
    }


async def _apply(fake, monkeypatch, *, current="0.5.0", running=False, spawn_ok=True):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("skillery_cli.core.identity.device_uid", lambda: "cdid-1")
    monkeypatch.setattr("skillery_cli.__version__", current, raising=False)
    monkeypatch.setattr(m, "_upgrade_already_running", lambda: running)
    monkeypatch.setattr(m, "_spawn_background_upgrade", lambda *a, **k: spawn_ok)
    ilog = ls.install_logger("daemon.log")
    await m._apply_device_tasks(
        fake, list(fake._device_tasks), rlog=ls.get_logger("reconcile"), ilog=ilog
    )


class TestDeviceTaskIsVisibleOnDefaultLevel:
    async def test_started_upgrade_is_logged_info(self, daemon_log, monkeypatch) -> None:
        fake = _FakeClient([_task("0.9.9")])
        await _apply(fake, monkeypatch)

        infos = [r for r in _records(daemon_log) if r["level"] == "INFO"]
        assert any("cli_upgrade запущен" in r["message"] for r in infos), (
            "успех device-задачи не виден на стандартном уровне демона"
        )
        assert fake.task_reports and fake.task_reports[0]["status"] == "applied"

    async def test_already_on_target_is_logged_info(self, daemon_log, monkeypatch) -> None:
        fake = _FakeClient([_task("0.5.0")])
        await _apply(fake, monkeypatch, current="0.5.0")

        infos = [r for r in _records(daemon_log) if r["level"] == "INFO"]
        assert any("уже на целевой версии" in r["message"] for r in infos)
        assert fake.task_reports[0]["status"] == "applied"

    async def test_unsupported_task_type_skip_is_logged_info(
        self, daemon_log, monkeypatch
    ) -> None:
        fake = _FakeClient([_task(ttype="skill_update")])
        await _apply(fake, monkeypatch)

        infos = [r for r in _records(daemon_log) if r["level"] == "INFO"]
        assert any("пока не поддержана" in r["message"] for r in infos)
        assert fake.task_reports == [], "задел не должен слать терминальный статус"


class TestUpgradeLockSkipIsLoud:
    async def test_deferred_by_upgrade_lock_is_visible_and_not_terminal(
        self, daemon_log, monkeypatch
    ) -> None:
        """Апгрейд уже идёт → заметная INFO «отложено», без терминального рапорта.

        Раньше пропуск был тихим (ACCESS): задача висела в ``delivered``
        неопределённо долго и никто не знал почему.
        """
        fake = _FakeClient([_task("0.9.9")])
        await _apply(fake, monkeypatch, running=True)

        infos = [r for r in _records(daemon_log) if r["level"] == "INFO"]
        deferred = [r for r in infos if "отложен" in r["message"]]
        assert deferred, "пропуск по апгрейд-локу остался тихим"
        assert (deferred[0].get("context") or {}).get("status") == "deferred"
        # Терминальный статус НЕ шлём — backend переотдаст задачу следующим тактом.
        assert fake.task_reports == []

    async def test_task_is_applied_on_next_tick_when_lock_released(
        self, daemon_log, monkeypatch
    ) -> None:
        """Отложенная задача действительно применяется, когда лок освободился."""
        fake = _FakeClient([_task("0.9.9")])
        await _apply(fake, monkeypatch, running=True)
        assert fake.task_reports == []

        await _apply(fake, monkeypatch, running=False)
        assert [r["status"] for r in fake.task_reports] == ["applied"]


class TestAccessTokenIsNotShadowed:
    async def test_reconcile_device_queue_passes_real_token(
        self, tmp_path, monkeypatch
    ) -> None:
        """Регресс: в HubClient должен уйти ТОКЕН, а не функция логирования."""
        from skillery_cli.config import ClientConfig

        cfg = ClientConfig.load()
        monkeypatch.setattr(
            type(cfg), "effective_store_dir", lambda self: tmp_path / "store"
        )
        (tmp_path / "store").mkdir(parents=True, exist_ok=True)

        seen: dict = {}
        fake = _FakeClient([])

        def _factory(**kw):  # type: ignore[no-untyped-def]
            seen.update(kw)
            return fake

        monkeypatch.setattr(m, "HubClient", _factory)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda c: None)

        class _Agent:
            name = "claude"

        await m._reconcile_device_queue(
            cfg, "REAL-TOKEN", channel="published", agent_target=_Agent(), wait=0
        )

        assert seen.get("access_token") == "REAL-TOKEN", (
            "access-токен подменён (локальный импорт перекрыл параметр)"
        )

    async def test_install_chain_gets_real_token(self, tmp_path, monkeypatch) -> None:
        """Тот же регресс на пути установки навыка из веб-очереди."""
        from skillery_cli.config import ClientConfig

        cfg = ClientConfig.load()
        monkeypatch.setattr(
            type(cfg), "effective_store_dir", lambda self: tmp_path / "store"
        )
        (tmp_path / "store").mkdir(parents=True, exist_ok=True)

        class _Queue(_FakeClient):
            async def fetch_device_queue_full(self, **kw):  # type: ignore[no-untyped-def]
                return {
                    "items": [{"slug": "alpha", "desired_version": "1.0.0"}],
                    "device_tasks": [],
                }

            async def report_device_apply(self, **kw):  # type: ignore[no-untyped-def]
                return {"ok": True}

            async def report_cli_log(self, **kw):  # type: ignore[no-untyped-def]
                return None

        monkeypatch.setattr(m, "HubClient", lambda **kw: _Queue())
        monkeypatch.setattr(m, "_make_refresh_callback", lambda c: None)
        got: list[object] = []

        async def _chain(cfg_, access, **kw):  # type: ignore[no-untyped-def]
            got.append(access)
            return []

        monkeypatch.setattr(m, "_install_chain", _chain)
        monkeypatch.setattr(m, "read_meta", lambda p: {"version": "1.0.0"})

        class _Agent:
            name = "claude"

        await m._reconcile_device_queue(
            cfg, "REAL-TOKEN", channel="published", agent_target=_Agent(), wait=0
        )

        assert got == ["REAL-TOKEN"]
