"""Автообновление САМОГО CLI: проверка версии на PyPI, уведомление, `upgrade`."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from skillery_cli import __main__ as main_mod
from skillery_cli.config import ClientConfig


# ---------------- _fetch_latest_pypi_version ----------------
def test_fetch_latest_parses_info_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json

    payload = json.dumps({"info": {"version": "9.9.9"}}).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=0: _Resp(payload)
    )
    assert main_mod._fetch_latest_pypi_version("skillery-cli") == "9.9.9"


def test_fetch_latest_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url, timeout=0):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert main_mod._fetch_latest_pypi_version("skillery-cli") is None


# ---------------- _check_cli_update ----------------
def test_check_cli_update_disabled_returns_none() -> None:
    cfg = ClientConfig(base_url="x", cli_update_check=False)
    assert main_mod._check_cli_update(cfg) is None


def test_check_cli_update_fetches_and_caches_when_newer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skillery_cli.__version__", "1.0.0")
    monkeypatch.setattr(main_mod, "_fetch_latest_pypi_version", lambda pkg, **k: "1.2.0")
    saved: dict[str, Any] = {}
    monkeypatch.setattr(ClientConfig, "save", lambda self: saved.update({"at": self.cli_update_check_at, "ver": self.cli_latest_version}))

    cfg = ClientConfig(base_url="x")
    assert main_mod._check_cli_update(cfg) == "1.2.0"
    assert saved["ver"] == "1.2.0"  # закэшировано
    assert saved["at"]  # таймстамп проставлен


def test_check_cli_update_uses_cache_within_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В пределах 24ч PyPI НЕ опрашивается — ответ из кэша."""
    monkeypatch.setattr("skillery_cli.__version__", "1.0.0")
    called = {"n": 0}

    def _spy(pkg, **k):
        called["n"] += 1
        return "9.9.9"

    monkeypatch.setattr(main_mod, "_fetch_latest_pypi_version", _spy)
    cfg = ClientConfig(
        base_url="x",
        cli_update_check_at=datetime.now(UTC).isoformat(),
        cli_latest_version="1.5.0",
    )
    assert main_mod._check_cli_update(cfg) == "1.5.0"  # из кэша
    assert called["n"] == 0  # PyPI не трогали


def test_check_cli_update_none_when_not_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skillery_cli.__version__", "2.0.0")
    monkeypatch.setattr(main_mod, "_fetch_latest_pypi_version", lambda pkg, **k: "1.0.0")
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    assert main_mod._check_cli_update(ClientConfig(base_url="x")) is None


# ---------------- _detect_upgrade_command ----------------
@pytest.mark.parametrize(
    "exe,which,expected_head",
    [
        ("/home/u/.local/share/uv/tools/skillery-cli/bin/python", {"uv"}, ["uv", "tool", "upgrade"]),
        ("/home/u/.local/pipx/venvs/skillery-cli/bin/python", {"pipx"}, ["pipx", "upgrade"]),
        ("/usr/bin/python3", set(), None),  # ни uv ни pipx → pip
    ],
)
def test_detect_upgrade_command(
    monkeypatch: pytest.MonkeyPatch, exe, which, expected_head
) -> None:
    monkeypatch.setattr(main_mod.sys, "executable", exe)
    monkeypatch.setattr("shutil.which", lambda name: name if name in which else None)
    cmd = main_mod._detect_upgrade_command()
    if expected_head is None:
        assert cmd[1:] == ["-m", "pip", "install", "--upgrade", "skillery-cli"]
    else:
        assert cmd[: len(expected_head)] == expected_head
        assert cmd[-1] == "skillery-cli"


# ---------------- _maybe_notify_cli_update: JSON-режим молчит ----------------
def test_notify_silent_in_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli import output as out_mod

    called = {"n": 0}
    monkeypatch.setattr(
        main_mod, "_check_cli_update_detailed",
        lambda cfg: (called.__setitem__("n", called["n"] + 1) or ("9.9.9", False)),
    )
    old = out_mod._mode
    try:
        out_mod._mode = "json"
        main_mod._maybe_notify_cli_update(ClientConfig(base_url="x"))
        assert called["n"] == 0  # в JSON-режиме даже не проверяем
        out_mod._mode = "text"
        main_mod._maybe_notify_cli_update(ClientConfig(base_url="x"))
        assert called["n"] == 1  # в text-режиме проверка идёт
    finally:
        out_mod._mode = old


# ---------------- cmd_upgrade --check ----------------
def test_cmd_upgrade_check_reports_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skillery_cli.__version__", "1.0.0")
    monkeypatch.setattr(main_mod, "_fetch_latest_pypi_version", lambda pkg, **k: "1.3.0")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: ClientConfig(base_url="x")))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(main_mod, "emit_data", lambda payload, **k: captured.update(payload))

    main_mod.cmd_upgrade(check=True)
    assert captured["update_available"] is True
    assert captured["latest"] == "1.3.0"
    assert captured["current"] == "1.0.0"


# ---------------- _check_cli_update_detailed: freshness ----------------
def test_check_detailed_fresh_on_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skillery_cli.__version__", "1.0.0")
    monkeypatch.setattr(main_mod, "_fetch_latest_pypi_version", lambda pkg, **k: "1.2.0")
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    v, fresh = main_mod._check_cli_update_detailed(ClientConfig(base_url="x"))
    assert v == "1.2.0" and fresh is True


def test_check_detailed_not_fresh_within_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skillery_cli.__version__", "1.0.0")
    cfg = ClientConfig(
        base_url="x",
        cli_update_check_at=datetime.now(UTC).isoformat(),
        cli_latest_version="1.5.0",
    )
    v, fresh = main_mod._check_cli_update_detailed(cfg)
    assert v == "1.5.0" and fresh is False  # из кэша, не свежая


# ---------------- авто-self-update поведение ----------------
def _force_text_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli import output as out
    monkeypatch.setattr(out, "_mode", "text", raising=False)


def test_auto_upgrades_on_fresh_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """cli_auto_upgrade=True + свежая проверка → тихо спавним обновление в фоне."""
    _force_text_mode(monkeypatch)
    monkeypatch.setattr(main_mod, "_check_cli_update_detailed", lambda cfg: ("2.0.0", True))
    spawned = {"n": 0}
    monkeypatch.setattr(
        main_mod, "_spawn_background_upgrade",
        lambda: (spawned.__setitem__("n", spawned["n"] + 1) or True),
    )
    main_mod._maybe_notify_cli_update(ClientConfig(base_url="x", cli_auto_upgrade=True))
    assert spawned["n"] == 1


def test_no_autoupgrade_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_text_mode(monkeypatch)
    monkeypatch.setattr(main_mod, "_check_cli_update_detailed", lambda cfg: ("2.0.0", True))
    spawned = {"n": 0}
    monkeypatch.setattr(
        main_mod, "_spawn_background_upgrade",
        lambda: (spawned.__setitem__("n", spawned["n"] + 1) or True),
    )
    main_mod._maybe_notify_cli_update(ClientConfig(base_url="x", cli_auto_upgrade=False))
    assert spawned["n"] == 0  # авто выключено → только уведомление


def test_no_autoupgrade_when_not_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """cached-newer (fresh=False) → не спавним на каждой команде, только уведомление."""
    _force_text_mode(monkeypatch)
    monkeypatch.setattr(main_mod, "_check_cli_update_detailed", lambda cfg: ("2.0.0", False))
    spawned = {"n": 0}
    monkeypatch.setattr(
        main_mod, "_spawn_background_upgrade",
        lambda: (spawned.__setitem__("n", spawned["n"] + 1) or True),
    )
    main_mod._maybe_notify_cli_update(ClientConfig(base_url="x", cli_auto_upgrade=True))
    assert spawned["n"] == 0


def test_spawn_background_upgrade_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd
            calls["kw"] = kw

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    monkeypatch.setattr(
        main_mod, "_detect_upgrade_command",
        lambda: ["uv", "tool", "upgrade", "skillery-cli"],
    )
    assert main_mod._spawn_background_upgrade() is True
    assert calls["cmd"] == ["uv", "tool", "upgrade", "skillery-cli"]
    import subprocess
    assert calls["kw"]["stdout"] == subprocess.DEVNULL  # detached, без вывода


# ---------------- self-heal: _invoke_app при исключении ----------------
def test_invoke_app_self_heals_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неожиданное исключение → доктор чинит → ретрай (успех), без raw-traceback."""
    calls = {"n": 0}

    def _fake_app():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # первый вызов падает
        raise SystemExit(0)  # после починки — успех

    monkeypatch.setattr(main_mod, "app", _fake_app)
    monkeypatch.setattr(main_mod, "_self_heal_repairs", lambda: ["починил base_url"])
    monkeypatch.setattr(main_mod, "_write_crash_log", lambda exc: __import__("pathlib").Path("x"))
    with pytest.raises(SystemExit) as e:
        main_mod._invoke_app(retry=True)
    assert e.value.code == 0  # ретрай удался
    assert calls["n"] == 2  # первый + ретрай


def test_invoke_app_logs_when_unrepairable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нечего чинить → пишем log + exit 1, НЕ роняем raw-traceback."""
    def _fake_app():
        raise RuntimeError("boom")

    logged = {"n": 0}
    monkeypatch.setattr(main_mod, "app", _fake_app)
    monkeypatch.setattr(main_mod, "_self_heal_repairs", lambda: [])  # нечего чинить
    monkeypatch.setattr(
        main_mod, "_write_crash_log",
        lambda exc: (logged.__setitem__("n", 1) or __import__("pathlib").Path("log")),
    )
    with pytest.raises(SystemExit) as e:
        main_mod._invoke_app(retry=True)
    assert e.value.code == 1
    assert logged["n"] == 1  # лог записан


def test_invoke_app_passes_systemexit_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Штатный SystemExit (typer) проходит насквозь без доктора."""
    healed = {"n": 0}
    monkeypatch.setattr(main_mod, "app", lambda: (_ for _ in ()).throw(SystemExit(2)))
    monkeypatch.setattr(main_mod, "_self_heal_repairs", lambda: healed.__setitem__("n", 1) or [])
    with pytest.raises(SystemExit) as e:
        main_mod._invoke_app(retry=True)
    assert e.value.code == 2
    assert healed["n"] == 0  # доктор НЕ звался
