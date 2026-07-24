"""C2: инициатор действия (cli / web-queue / daemon-auto) + причина провала.

Владелец: в логе виден ИНИЦИАТОР (сам CLI vs веб→демон подобрал задачу vs
daemon-auto) и ПРИЧИНА, почему навык не установился. Здесь закреплено:
- ``_reconcile_device_queue`` ставит initiator=``web-queue`` (веб-очередь);
- ``_auto_update_hub_installs`` ставит initiator=``daemon-auto``;
- ``_install_chain`` по умолчанию initiator=``cli`` и пишет его в аудит
  материализации;
- провал install пишет ERROR с причиной в лог (cli.log / daemon.log).
"""
from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path

import pytest

from skillery_cli import __main__ as m
from skillery_cli.core import logging_setup as ls

_LOGGER_NAMES = ("skillery", "skillery.install", "skillery.daemon", "skillery.reconcile")


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


def _records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


class _Agent:
    name = "claude"


class _FakeClient:
    def __init__(self, queue: list[dict] | None = None) -> None:
        self._queue = queue or []
        self.reports: list[dict] = []
        self.closed = False

    async def fetch_device_queue(self, **kw):  # type: ignore[no-untyped-def]
        return list(self._queue)

    async def report_device_apply(self, **kw):  # type: ignore[no-untyped-def]
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


# --------------------------------------------------------------------------- #
# initiator на call-site'ах
# --------------------------------------------------------------------------- #
class TestInitiatorPropagation:
    async def test_device_queue_marks_web_queue(self, cfg, monkeypatch) -> None:
        captured: dict = {}

        async def _chain(*a, **kw):  # type: ignore[no-untyped-def]
            captured.update(kw)

        fake = _FakeClient([{"slug": "atlas", "desired_version": "1.0.0", "skill_id": 3}])
        monkeypatch.setattr(m, "HubClient", lambda **kw: fake)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
        monkeypatch.setattr(m, "_install_chain", _chain)
        monkeypatch.setattr(m, "read_meta", lambda p: {"version": "1.0.0"})

        await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )
        assert captured.get("initiator") == "web-queue"

    async def test_auto_update_marks_daemon_auto(self, cfg, monkeypatch) -> None:
        captured: dict = {}

        async def _chain(*a, **kw):  # type: ignore[no-untyped-def]
            captured.update(kw)

        class _Bundles(_FakeClient):
            async def install_bundle(self, ref, channel):  # type: ignore[no-untyped-def]
                return {"version": "2.0.0", "repo_url": "https://x/repo.git"}

        fake = _Bundles()
        monkeypatch.setattr(m, "HubClient", lambda **kw: fake)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
        monkeypatch.setattr(m, "_install_chain", _chain)
        monkeypatch.setattr(
            m, "_collect_store_skills",
            lambda root: [{"ref": "atlas", "skill_id": 3, "source": "hub", "version": "1.0.0"}],
        )
        monkeypatch.setattr(m, "read_meta", lambda p: {"version": "2.0.0"})
        monkeypatch.setattr(m, "_report_auto_updates", lambda *a, **k: _noop())
        monkeypatch.setattr(m, "_touch_auto_update_cooldown", lambda cfg: None)
        cfg.auto_update = True
        cfg.last_auto_update_at = None

        rep = await m._auto_update_hub_installs(
            cfg, "tok", agent_target=_Agent(), channel="published"
        )
        assert rep["updated"] == ["atlas"]
        assert captured.get("initiator") == "daemon-auto"


async def _noop() -> None:
    return None


# --------------------------------------------------------------------------- #
# _install_chain: аудит материализации несёт initiator; провал → ERROR
# --------------------------------------------------------------------------- #
class TestInstallChainAudit:
    def _wire(self, monkeypatch, tmp_path, *, bundle_boom: bool = False):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
        monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)

        class _Client:
            async def install_bundle(self, slug, channel):  # type: ignore[no-untyped-def]
                if bundle_boom:
                    raise RuntimeError("bundle 404 приватный репо")
                return {
                    "skill_slug": slug, "version": "1.0.0",
                    "repo_url": "https://x/repo.git", "manifest": {},
                    "commit_sha": "abc123", "skill_id": 7,
                }

            async def close(self) -> None:
                pass

        monkeypatch.setattr(m, "HubClient", lambda **kw: _Client())
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
        monkeypatch.setattr(m, "SkillInstaller", lambda *a, **k: object())

        result = type("R", (), {
            "skill_id": 7, "is_update": False, "target_dir": tmp_path / "t",
            "scope": "global", "linked": True, "link_kind": "symlink",
            "content": "git", "skipped": False, "skip_reason": None,
            "store_dir": tmp_path / "store" / "atlas",
        })()

        async def _mat(*a, **k):  # type: ignore[no-untyped-def]
            return result

        monkeypatch.setattr(m, "_materialize_from_bundle", _mat)
        monkeypatch.setattr(m, "_merge_tooling_from_store", lambda man, sd: {})
        monkeypatch.setattr(m, "_apply_tooling", lambda *a, **k: None)
        monkeypatch.setattr(m, "_emit_onboarding", lambda *a, **k: None)
        monkeypatch.setattr(m, "track_skill_event", lambda *a, **k: None)

    async def test_materialize_audit_defaults_to_cli(self, tmp_path, monkeypatch) -> None:
        from skillery_cli.config import ClientConfig

        self._wire(monkeypatch, tmp_path)
        await m._install_chain(
            ClientConfig.load(), "tok", slug="atlas", channel="published",
            scope="global", project_path=None, force=False, agent_target=_Agent(),
        )
        recs = _records(tmp_path / "cli.log")
        mat = [r for r in recs if r.get("context", {}).get("step") == "materialize"]
        assert mat and mat[0]["context"]["initiator"] == "cli"

    async def test_materialize_audit_carries_web_queue(self, tmp_path, monkeypatch) -> None:
        from skillery_cli.config import ClientConfig

        self._wire(monkeypatch, tmp_path)
        await m._install_chain(
            ClientConfig.load(), "tok", slug="atlas", channel="published",
            scope="global", project_path=None, force=False, agent_target=_Agent(),
            initiator="web-queue", headless=True,
        )
        recs = _records(tmp_path / "daemon.log")
        mat = [r for r in recs if r.get("context", {}).get("step") == "materialize"]
        assert mat and mat[0]["context"]["initiator"] == "web-queue"

    async def test_failure_logs_error_with_reason(self, tmp_path, monkeypatch) -> None:
        from skillery_cli.config import ClientConfig

        self._wire(monkeypatch, tmp_path, bundle_boom=True)
        with pytest.raises(RuntimeError):
            await m._install_chain(
                ClientConfig.load(), "tok", slug="atlas", channel="published",
                scope="global", project_path=None, force=False, agent_target=_Agent(),
            )
        recs = _records(tmp_path / "cli.log")
        errs = [r for r in recs if r["level"] == "ERROR"]
        assert errs, "провал install обязан оставить ERROR с причиной"
        assert any("bundle 404" in json.dumps(r, ensure_ascii=False) for r in errs)


# --------------------------------------------------------------------------- #
# _reconcile_device_queue: провал → ERROR c текстом exc в daemon.log
# --------------------------------------------------------------------------- #
class TestDeviceQueueFailureLog:
    async def test_failure_writes_error_to_daemon_log(
        self, cfg, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
        monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)

        fake = _FakeClient([{"slug": "atlas", "desired_version": "0.4.0"}])
        monkeypatch.setattr(m, "HubClient", lambda **kw: fake)
        monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)

        async def _chain(*a, **kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk full")

        monkeypatch.setattr(m, "_install_chain", _chain)
        monkeypatch.setattr(m, "read_meta", lambda p: {})

        rep = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent()
        )
        assert rep["failed"] == ["atlas"]
        # провал по-прежнему рапортуется на бэк (не тронуто)
        assert fake.reports and fake.reports[0]["ok"] is False
        # но теперь ещё и локальный ERROR-след в daemon.log с причиной+инициатором
        recs = _records(tmp_path / "daemon.log")
        errs = [r for r in recs if r["level"] == "ERROR"]
        assert errs, "провал install из веб-очереди обязан быть виден в daemon.log"
        assert any("disk full" in json.dumps(r, ensure_ascii=False) for r in errs)
        assert any(r.get("context", {}).get("initiator") == "web-queue" for r in errs)
