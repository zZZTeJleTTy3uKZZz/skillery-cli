"""#1102: каденс-fallback авто-апгрейда CLI в демоне (если push не сработал).

Слои надёжности вокруг push-задачи ``cli_upgrade``:
- **long-poll** (основной) — device_tasks приходят опросом очереди (см.
  ``test_device_tasks_reconcile``);
- **раз в час** — периодический self-check PyPI в цикле демона (не на каждый такт);
- **при старте демона** — принудительный self-check (конвергируем на известный
  latest, даже если PyPI в этом вызове не опрашивался).

Инварианты: PyPI не спамим (кэш+cooldown в ``_check_cli_update_detailed``);
параллельных апгрейдов не плодим (``_upgrade_already_running``).
"""
from __future__ import annotations

from skillery_cli import __main__ as m
from skillery_cli.config import ClientConfig


# --------------------------------------------------------------------------- #
# _daemon_cli_self_upgrade: гейты spawn'а
# --------------------------------------------------------------------------- #
class TestSelfUpgradeGate:
    def _spy_spawn(self, monkeypatch):  # type: ignore[no-untyped-def]
        spawned: list[str] = []
        monkeypatch.setattr(
            m, "_spawn_background_upgrade",
            lambda *a, **k: (spawned.append(k.get("version")) or True),
        )
        monkeypatch.setattr(m, "_upgrade_already_running", lambda: False)
        return spawned

    async def test_startup_force_spawns_from_cache_even_if_not_fresh(
        self, monkeypatch
    ) -> None:
        """force=True (старт демона) → спавним из кэша, даже без свежей PyPI-проверки."""
        spawned = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(m, "_check_cli_update_detailed", lambda cfg: ("2.0.0", False))

        ok = await m._daemon_cli_self_upgrade(
            ClientConfig(base_url="x", cli_auto_upgrade=True), force=True
        )

        assert ok is True
        assert spawned == ["2.0.0"]

    async def test_periodic_does_not_spawn_when_not_fresh(self, monkeypatch) -> None:
        """force=False + не свежая проверка → НЕ спавним (не на каждый такт)."""
        spawned = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(m, "_check_cli_update_detailed", lambda cfg: ("2.0.0", False))

        ok = await m._daemon_cli_self_upgrade(
            ClientConfig(base_url="x", cli_auto_upgrade=True), force=False
        )

        assert ok is False
        assert spawned == []

    async def test_periodic_spawns_on_fresh_check(self, monkeypatch) -> None:
        """force=False + свежая PyPI-проверка нашла новее → спавним (суточный триггер)."""
        spawned = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(m, "_check_cli_update_detailed", lambda cfg: ("2.0.0", True))

        ok = await m._daemon_cli_self_upgrade(
            ClientConfig(base_url="x", cli_auto_upgrade=True), force=False
        )

        assert ok is True
        assert spawned == ["2.0.0"]

    async def test_disabled_auto_upgrade_never_spawns(self, monkeypatch) -> None:
        spawned = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(m, "_check_cli_update_detailed", lambda cfg: ("2.0.0", True))

        ok = await m._daemon_cli_self_upgrade(
            ClientConfig(base_url="x", cli_auto_upgrade=False), force=True
        )

        assert ok is False
        assert spawned == []

    async def test_no_newer_version_is_noop(self, monkeypatch) -> None:
        spawned = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(m, "_check_cli_update_detailed", lambda cfg: (None, True))

        ok = await m._daemon_cli_self_upgrade(
            ClientConfig(base_url="x", cli_auto_upgrade=True), force=True
        )

        assert ok is False
        assert spawned == []

    async def test_dedup_when_upgrade_already_running(self, monkeypatch) -> None:
        """Апгрейд уже идёт → не плодим второй (битый trampoline)."""
        spawned: list[str] = []
        monkeypatch.setattr(
            m, "_spawn_background_upgrade",
            lambda *a, **k: (spawned.append(k.get("version")) or True),
        )
        monkeypatch.setattr(m, "_upgrade_already_running", lambda: True)
        monkeypatch.setattr(m, "_check_cli_update_detailed", lambda cfg: ("2.0.0", True))

        ok = await m._daemon_cli_self_upgrade(
            ClientConfig(base_url="x", cli_auto_upgrade=True), force=True
        )

        assert ok is False
        assert spawned == []


# --------------------------------------------------------------------------- #
# Каденс в цикле демона: старт → force, далее раз в час
# --------------------------------------------------------------------------- #
async def test_daemon_loop_startup_then_hourly_cadence(monkeypatch) -> None:
    """Первый такт → принудительный self-check; далее не чаще раза в час."""
    import skillery_cli.commands.daemon as daemon_mod
    import skillery_cli.config as config_mod
    import skillery_cli.core.agents as agents_mod

    cfg = ClientConfig(base_url="http://localhost:8000")
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(config_mod, "load_tokens", lambda email: ("tok", "rt"))
    monkeypatch.setattr(agents_mod, "get_target", lambda name: object())

    # Все reconcile-проходы — no-op, кроме учёта self-upgrade вызовов.
    async def _noop(*a, **k):
        return {}

    monkeypatch.setattr(m, "_reconcile_device_queue", _noop)
    monkeypatch.setattr(m, "_reconcile_hub_installs", _noop)
    monkeypatch.setattr(m, "_auto_update_hub_installs", _noop)

    forces: list[bool] = []

    async def _self_upgrade(cfg, *, force):
        forces.append(force)
        return False

    monkeypatch.setattr(m, "_daemon_cli_self_upgrade", _self_upgrade)

    # Управляемое монотонное время через holder (robust к вызовам monotonic из
    # asyncio): тест двигает часы МЕЖДУ тактами, а не полагается на счётчик вызовов.
    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    runner = daemon_mod._build_runner(interval_seconds=60)
    assert runner._reconcile is not None

    clock["t"] = 0.0
    await runner._reconcile()  # старт → принудительная проверка
    clock["t"] = 100.0
    await runner._reconcile()  # +100с (в пределах часа) → пропуск
    clock["t"] = 4000.0
    await runner._reconcile()  # прошёл час → периодическая проверка

    assert forces == [True, False]
