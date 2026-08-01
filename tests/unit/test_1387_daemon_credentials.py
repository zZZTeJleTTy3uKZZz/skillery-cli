"""#1387: демон подхватывает новые креды сам и не молотит вхолостую на 401.

Инцидент, который эти тесты закрывают: демон, поднятый 30.07, держал в памяти
протухший токен; владелец сделал ``skillery auth login``, конфиг обновился,
команды заработали — а демон продолжал слать со старым токеном
(``cycles=47808 sent=7292 accepted=0``, outbox 739 строк) и не сказал об этом
ни в логе, ни в ``status``, ни в ``doctor``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import skillery_cli.__main__  # noqa: F401 — импорт ДО подмены ClientConfig.load
from skillery_cli.commands import daemon as daemon_cmd
from skillery_cli.commands import doctor as doctor_mod
from skillery_cli.daemon import credentials as creds_mod
from skillery_cli.daemon.credentials import (
    AUTH_NEEDS_LOGIN,
    AUTH_OK,
    DaemonCredentials,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def creds_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Изолированные файлы кред + фабрика ``DaemonCredentials`` на них."""
    cfg_path = tmp_path / "config.toml"
    tokens_path = tmp_path / "tokens.toml"
    store: dict[str, tuple[str | None, str | None]] = {"tokens": (None, None)}

    class _Cfg:
        def __init__(self, email: str | None) -> None:
            self.user_email = email
            self.base_url = "https://hub.test"
            self.agent = "claude_code"
            self.permissions = ["skill.read"] if email else []

        def is_logged_in(self) -> bool:
            return bool(self.user_email and self.permissions)

    def _load(path: Any = None) -> _Cfg:
        if not cfg_path.exists():
            return _Cfg(None)
        return _Cfg(json.loads(cfg_path.read_text(encoding="utf-8"))["email"])

    import skillery_cli.config as config_mod

    monkeypatch.setattr(config_mod.ClientConfig, "load", staticmethod(_load))
    monkeypatch.setattr(config_mod, "_default_config_file", lambda: cfg_path)
    monkeypatch.setattr(config_mod, "_tokens_file_path", lambda: tokens_path)
    monkeypatch.setattr(
        config_mod, "load_tokens", lambda email: store["tokens"]
    )

    def _login(email: str, access: str, refresh: str = "r") -> None:
        """Имитация `skillery auth login`: конфиг переписан, токены в сторе."""
        cfg_path.write_text(json.dumps({"email": email}), encoding="utf-8")
        store["tokens"] = (access, refresh)

    clock = _Clock()

    def _make(**kw: Any) -> DaemonCredentials:
        kw.setdefault("state_path", tmp_path / "daemon.auth.json")
        kw.setdefault("monotonic", clock)
        return DaemonCredentials(**kw)

    return type(
        "Env", (), {
            "make": staticmethod(_make), "login": staticmethod(_login),
            "clock": clock, "state_path": tmp_path / "daemon.auth.json",
            "cfg_path": cfg_path, "store": store,
        },
    )


# ── (а) подхват новых кред без ручного перезапуска ──────────────────────────
def test_new_login_is_picked_up_without_restart(creds_env) -> None:
    """Логин ВО ВРЕМЯ работы демона меняет токен у живого объекта кред."""
    creds_env.login("old@test", "TOKEN-OLD")
    creds = creds_env.make()
    assert creds.access() == "TOKEN-OLD"

    creds_env.login("new@test", "TOKEN-NEW")  # пользователь вошёл заново
    assert creds.access() == "TOKEN-NEW", "демон обязан подхватить сам"
    assert creds.config().user_email == "new@test"


def test_unchanged_credentials_are_not_re_read(creds_env, monkeypatch) -> None:
    """Пока файлы не менялись — дорогого чтения (keyring) нет: только stat."""
    creds_env.login("a@test", "T1")
    creds = creds_env.make()
    creds.access()
    calls = {"n": 0}
    import skillery_cli.config as config_mod

    def _counted(email):
        calls["n"] += 1
        return creds_env.store["tokens"]

    monkeypatch.setattr(config_mod, "load_tokens", _counted)
    for _ in range(50):
        creds.access()
    assert calls["n"] == 0, "перечитывать креды каждый такт — не наш механизм"


def test_login_clears_needs_login_state(creds_env) -> None:
    """Из «нужен вход» демон выходит сам по факту логина, без stop/start."""
    creds_env.login("a@test", "T1")
    creds = creds_env.make()
    creds.mark_needs_login("refresh отклонён")
    assert creds.blocked() is True

    creds_env.login("a@test", "T2")  # владелец сделал login
    assert creds.blocked() is False
    assert creds.auth_state() == AUTH_OK
    assert creds.access() == "T2"


# ── (б) 401 без валидного refresh → видимое состояние, без молотьбы ─────────
async def test_failed_refresh_marks_needs_login(creds_env, monkeypatch) -> None:
    """Неудачный refresh = «нужен вход»: причина в файле состояния, не ConnectError."""
    creds_env.login("a@test", "T1")
    creds = creds_env.make()

    import skillery_cli.__main__ as main_mod

    async def _fail() -> None:
        return None

    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda cfg: _fail)
    monkeypatch.setitem(main_mod._REFRESH_FAILURE, "reason", "сессия отозвана сервером")

    assert await creds.refresh_callback()() is None
    assert creds.auth_state() == AUTH_NEEDS_LOGIN
    saved = json.loads(creds_env.state_path.read_text(encoding="utf-8"))
    assert saved["state"] == AUTH_NEEDS_LOGIN
    assert saved["reason"] == "сессия отозвана сервером"


async def test_successful_refresh_updates_token_and_clears_state(
    creds_env, monkeypatch
) -> None:
    creds_env.login("a@test", "T1")
    creds = creds_env.make()
    creds.mark_needs_login("временный отказ")

    import skillery_cli.__main__ as main_mod

    async def _okay() -> tuple[str, str]:
        return ("T-FRESH", "R-FRESH")

    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda cfg: _okay)
    assert await creds.refresh_callback()() == ("T-FRESH", "R-FRESH")
    assert creds.auth_state() == AUTH_OK
    assert creds.access() == "T-FRESH"


def test_blocked_state_is_rechecked_after_window(creds_env) -> None:
    """Не «замолчал навсегда»: по истечении окна разрешается одна проба."""
    creds_env.login("a@test", "T1")
    creds = creds_env.make(recheck_after=900.0)
    creds.mark_needs_login("отказ")
    assert creds.blocked() is True

    creds_env.clock.now += 899
    assert creds.blocked() is True
    creds_env.clock.now += 2
    assert creds.blocked() is False, "окно истекло — обязаны попробовать"
    assert creds.blocked() is True, "проба выдана один раз, окно взведено заново"


def test_daemon_stops_building_clients_while_session_is_dead(
    creds_env, monkeypatch
) -> None:
    """Главный симптом: sent растёт при accepted=0. Клиент просто не строится."""
    creds_env.login("a@test", "T1")
    creds = creds_env.make()
    built: list[str] = []
    monkeypatch.setattr(
        daemon_cmd._common, "make_client",
        lambda cfg, access, **kw: built.append(access) or object(),
    )
    runner = daemon_cmd._build_runner(
        interval_seconds=60.0, reconcile_installs=False, credentials=creds,
    )
    factory = runner._sender._make_client
    assert factory(anonymous=False) is not None
    built.clear()

    creds.mark_needs_login("refresh отклонён")
    assert factory(anonymous=False) is None
    assert built == [], "на мёртвой сессии запрос не отправляется вовсе"

    creds_env.login("a@test", "T2")  # login возвращает демона в строй
    assert factory(anonymous=False) is not None
    assert built == ["T2"]


async def test_daemon_started_before_login_reconciles_after_it(
    creds_env, monkeypatch
) -> None:
    """Демон, поднятый ДО входа, после login опрашивает очередь, а не молчит.

    Обычный случай: автозапуск при загрузке машины. Раньше reconcile
    подключался по «залогинен на момент построения», а `ensure_daemon_running`
    при login живой процесс не перезапускает — очередь устройства не опрашивал
    вообще никто до перезагрузки.
    """
    creds = creds_env.make()  # конфига ещё нет → вход не выполнялся
    assert creds.config().is_logged_in() is False
    runner = daemon_cmd._build_runner(interval_seconds=60.0, credentials=creds)
    assert runner._reconcile is not None, "reconcile обязан быть подключён"

    polled: list[str] = []

    class _Stream:
        async def run_once(self, cfg, access, **kw):  # noqa: ANN001, ANN003
            polled.append(access)

    monkeypatch.setattr(
        daemon_cmd, "DeviceQueueStream", lambda **kw: _Stream()
    )
    runner = daemon_cmd._build_runner(interval_seconds=60.0, credentials=creds)
    await runner._reconcile()
    assert polled == [], "без кред опрашивать нечего"

    creds_env.login("a@test", "T1")
    import skillery_cli.__main__ as main_mod
    import skillery_cli.core.agents as agents_mod

    monkeypatch.setattr(agents_mod, "get_target", lambda name: object())

    async def _noop(*_a, **_kw):
        return {}

    monkeypatch.setattr(main_mod, "_reconcile_hub_installs", _noop)
    monkeypatch.setattr(main_mod, "_auto_update_hub_installs", _noop)
    monkeypatch.setattr(main_mod, "_daemon_cli_self_upgrade", _noop)
    await runner._reconcile()
    assert polled == ["T1"], "после login живой демон обязан взяться за очередь"


def test_daemon_client_carries_state_aware_refresh(creds_env, monkeypatch) -> None:
    """Демон отдаёт транспорту СВОЙ refresh-callback — иначе 401 не наблюдаем."""
    creds_env.login("a@test", "T1")
    creds = creds_env.make()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        daemon_cmd._common, "make_client",
        lambda cfg, access, **kw: seen.update(kw) or object(),
    )
    runner = daemon_cmd._build_runner(
        interval_seconds=60.0, reconcile_installs=False, credentials=creds,
    )
    runner._sender._make_client(anonymous=False)
    assert seen.get("on_refresh") is not None


# ── видимость: status и doctor ──────────────────────────────────────────────
def test_daemon_status_reports_needs_login(monkeypatch, tmp_path) -> None:
    payload: dict[str, Any] = {}
    monkeypatch.setattr(
        daemon_cmd, "read_auth_state",
        lambda *_a, **_kw: {"state": AUTH_NEEDS_LOGIN, "reason": "refresh отклонён"},
    )
    monkeypatch.setattr(daemon_cmd, "read_running_pid", lambda *_a, **_kw: None)
    monkeypatch.setattr(daemon_cmd, "read_state", lambda *_a, **_kw: {})
    monkeypatch.setattr(daemon_cmd, "_outbox_snapshot", lambda: ("/q", 739, {}))
    monkeypatch.setattr(
        daemon_cmd, "emit_data", lambda p, **kw: payload.update(p)
    )
    daemon_cmd.cmd_daemon_status()
    assert payload["needs_login"] is True
    assert "auth login" in payload["warning"]


def test_doctor_recognises_expired_session(monkeypatch) -> None:
    """`doctor` называет причину и точное действие вместо «починок не нашлось»."""
    monkeypatch.setattr(
        creds_mod, "read_auth_state",
        lambda *_a, **_kw: {"state": AUTH_NEEDS_LOGIN, "reason": "сессия отозвана"},
    )

    class _Cfg:
        user_email = "a@test"

    res = doctor_mod._probe_session(_Cfg())
    assert res.level == "fail"
    assert "сессия отозвана" in res.detail
    assert "auth login" in res.detail


def test_doctor_session_ok_when_state_is_clean(monkeypatch) -> None:
    monkeypatch.setattr(creds_mod, "read_auth_state", lambda *_a, **_kw: {})
    monkeypatch.setattr(doctor_mod, "_access_token_expired", lambda cfg: False)

    class _Cfg:
        user_email = "a@test"

    assert doctor_mod._probe_session(_Cfg()).level == "pass"
    assert doctor_mod._probe_session(type("C", (), {"user_email": None})()).level == "pass"


def test_doctor_warns_on_locally_expired_access_token(monkeypatch) -> None:
    monkeypatch.setattr(creds_mod, "read_auth_state", lambda *_a, **_kw: {})
    monkeypatch.setattr(doctor_mod, "_access_token_expired", lambda cfg: True)

    class _Cfg:
        user_email = "a@test"

    res = doctor_mod._probe_session(_Cfg())
    assert res.level == "warn"
    assert "auth login" in res.detail


def test_session_probe_is_wired_into_doctor(monkeypatch) -> None:
    """Проверка обязана быть в общем прогоне, а не только вызываться руками."""
    class _Cfg:
        user_email = None
        agent = "claude_code"

        def is_logged_in(self) -> bool:
            return False

    monkeypatch.setattr(doctor_mod, "_probe_python", lambda: doctor_mod.ok("py"))
    monkeypatch.setattr(doctor_mod, "_probe_package_manager", lambda: doctor_mod.ok("pm"))
    monkeypatch.setattr(doctor_mod, "_probe_config", lambda: doctor_mod.ok("cfg"))
    monkeypatch.setattr(doctor_mod, "_probe_agent", lambda c: doctor_mod.ok("agent"))
    monkeypatch.setattr(doctor_mod, "_probe_path_store", lambda: doctor_mod.ok("path"))
    monkeypatch.setattr(
        doctor_mod, "_probe_shim_collisions", lambda: doctor_mod.ok("collisions")
    )
    monkeypatch.setattr(doctor_mod, "_probe_clikit", lambda: doctor_mod.ok("clikit"))
    monkeypatch.setattr(doctor_mod, "_probe_cli_version", lambda: doctor_mod.ok("ver"))
    names = [r.name for r in doctor_mod.run_checks(_Cfg())]
    assert "Сессия" in names
