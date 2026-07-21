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
        ("/home/u/.local/share/uv/tools/skillery-cli/bin/python", {"uv"}, ["uv", "tool", "install"]),
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


def test_uv_upgrade_busts_index_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ``--refresh`` обновление молча не наступало.

    uv кэширует индекс PyPI: `uv tool upgrade` отвечал «уже последняя», хотя на
    PyPI лежала новее — пользователь «обновлялся» и оставался на старой версии.
    """
    monkeypatch.setattr(main_mod.sys, "executable", "/x/uv/tools/skillery-cli/bin/python")
    monkeypatch.setattr("shutil.which", lambda name: name if name == "uv" else None)

    assert "--refresh" in main_mod._detect_upgrade_command()


def test_uv_never_uses_tool_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uv tool upgrade` НЕ принимает `--refresh` — команда падает целиком.

    Ловили живьём: `error: unexpected argument '--refresh' found`. Без обхода
    кэша обновление не наступает, а с ним `tool upgrade` не запускается вовсе —
    поэтому uv-ветка обязана идти через `tool install --force --refresh`.
    """
    monkeypatch.setattr(main_mod.sys, "executable", "/x/uv/tools/skillery-cli/bin/python")
    monkeypatch.setattr("shutil.which", lambda name: name if name == "uv" else None)

    for cmd in main_mod._upgrade_commands("1.2.3") + [main_mod._detect_upgrade_command()]:
        assert cmd[:3] == ["uv", "tool", "install"], cmd
        assert "--refresh" in cmd, cmd


@pytest.mark.parametrize(
    "exe,which,expected",
    [
        (
            "/x/uv/tools/skillery-cli/bin/python",
            {"uv"},
            ["uv", "tool", "install", "--force", "--refresh", "skillery-cli==1.2.3"],
        ),
        (
            "/x/pipx/venvs/skillery-cli/bin/python",
            {"pipx"},
            ["pipx", "install", "--force", "skillery-cli==1.2.3"],
        ),
    ],
)
def test_known_version_is_pinned(
    monkeypatch: pytest.MonkeyPatch, exe, which, expected
) -> None:
    """Версию с PyPI уже знаем — пинуем её, не оставляя резолверу выбора.

    Иначе менеджер решал по своему кэшу, что обновлять нечего, и `upgrade`
    оказывался пустышкой.
    """
    monkeypatch.setattr(main_mod.sys, "executable", exe)
    monkeypatch.setattr("shutil.which", lambda name: name if name in which else None)

    assert main_mod._detect_upgrade_command("1.2.3") == expected


def test_known_version_is_pinned_for_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr("shutil.which", lambda name: None)

    cmd = main_mod._detect_upgrade_command("1.2.3")

    assert cmd[1:] == ["-m", "pip", "install", "--upgrade", "skillery-cli==1.2.3"]


def _capture_spawn(monkeypatch) -> dict:
    """Перехватить фоновый спавн апгрейда и вернуть {launcher, cmd, kw, config}.

    Новый контракт: спавнится ``[launcher, worker.py, config.json]``, а команды
    обновления и путь бинаря демона лежат в config.json (не в строке worker'а).
    """
    calls: dict[str, Any] = {}
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: calls.update(cmd=cmd, kw=kw))
    monkeypatch.setattr(main_mod, "_stop_daemon_for_upgrade", lambda: None)
    monkeypatch.setattr(main_mod, "_upgrade_already_running", lambda: False)

    def _read():
        import json

        cfg_path = calls["cmd"][2]
        calls["launcher"] = calls["cmd"][0]
        calls["config"] = json.loads(open(cfg_path, encoding="utf-8").read())
        return calls

    calls["read"] = _read
    return calls


def test_background_upgrade_passes_version_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Фоновый апгрейд обязан нести пин — иначе гарантия теряется по дороге."""
    cap = _capture_spawn(monkeypatch)

    assert main_mod._spawn_background_upgrade(delay=0, version="9.9.9") is True
    cap["read"]()

    commands = cap["config"]["commands"]
    assert any("skillery-cli==9.9.9" in c[-1] for c in commands)


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
        lambda *a, **k: (spawned.__setitem__("n", spawned["n"] + 1) or True),
    )
    main_mod._maybe_notify_cli_update(ClientConfig(base_url="x", cli_auto_upgrade=True))
    assert spawned["n"] == 1


def test_no_autoupgrade_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_text_mode(monkeypatch)
    monkeypatch.setattr(main_mod, "_check_cli_update_detailed", lambda cfg: ("2.0.0", True))
    spawned = {"n": 0}
    monkeypatch.setattr(
        main_mod, "_spawn_background_upgrade",
        lambda *a, **k: (spawned.__setitem__("n", spawned["n"] + 1) or True),
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
        lambda *a, **k: (spawned.__setitem__("n", spawned["n"] + 1) or True),
    )
    main_mod._maybe_notify_cli_update(ClientConfig(base_url="x", cli_auto_upgrade=True))
    assert spawned["n"] == 0


def test_spawn_background_upgrade_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture_spawn(monkeypatch)

    assert main_mod._spawn_background_upgrade(delay=0, version="1.2.3") is True
    cap["read"]()

    import subprocess

    # Спавнит ИНТЕРПРЕТАТОР + файл worker'а (не строку -c, не skillery.exe):
    # cmd = [launcher, worker.py, config.json]. Команды апгрейда — в config.
    assert cap["cmd"][1].endswith("_upgrade_worker.py")
    assert cap["cmd"][2].endswith("_upgrade_worker.json")
    assert cap["kw"]["stdout"] == subprocess.DEVNULL
    assert cap["config"]["commands"], "цепочка команд не должна быть пустой"


def test_worker_falls_back_when_pinned_version_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пин может транзиентно упасть — цепочка обязана добрать обычным upgrade.

    Сразу после релиза индекс PyPI ещё не разъехался по CDN, и `pkg==X.Y.Z`
    отвечает «no version». Без цепочки обновление сорвалось бы с ошибкой.
    """
    cap = _capture_spawn(monkeypatch)
    monkeypatch.setattr(main_mod.sys, "executable", "/x/uv/tools/skillery-cli/bin/python")
    monkeypatch.setattr("shutil.which", lambda name: name if name == "uv" else None)

    main_mod._spawn_background_upgrade(delay=0, version="1.2.3")
    commands = cap["read"]()["config"]["commands"]

    assert commands[0][-1] == "skillery-cli==1.2.3"   # сначала точная версия
    assert commands[-1][-1] == "skillery-cli"         # затем фолбэк без пина
    assert "--refresh" in commands[-1]


def test_worker_runs_outside_tool_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker НЕ должен запускаться питоном из каталога самого инструмента.

    `sys.executable` живёт в `…/tools/skillery-cli/Scripts/` и держит этот
    каталог открытым, пока worker жив → uv не может его удалить и падает
    «failed to remove directory Scripts: Отказано в доступе (os error 5)».
    Снаружи это выглядело как «обновление запущено» и полная тишина: worker
    блокировал сам себя. Берём базовый интерпретатор — он вне tool-каталога.
    """
    cap = _capture_spawn(monkeypatch)
    monkeypatch.setattr(
        main_mod.sys, "executable", "/x/uv/tools/skillery-cli/Scripts/python.exe"
    )
    monkeypatch.setattr(main_mod.sys, "base_prefix", "/x/uv/python/cpython-3.14")
    monkeypatch.setattr(main_mod.Path, "exists", lambda self: True)

    main_mod._spawn_background_upgrade(delay=0)
    launcher = cap["read"]()["launcher"]

    assert "tools" not in launcher.replace("\\", "/").split("/")
    assert launcher != main_mod.sys.executable


class TestNoVisibleConsoleWindows:
    """Апгрейд не должен показывать НИ ОДНОГО окна.

    Само подавление окна у ВНУТРЕННИХ вызовов теперь живёт в worker'е и покрыто
    ``tests/test_upgrade_worker.py`` (worker detached ⇒ консоли нет, и без
    CREATE_NO_WINDOW консольный uv всплыл бы вкладкой Windows Terminal). Здесь
    держим инварианты СПАВНА worker'а из CLI.
    """

    def test_outer_does_not_mix_exclusive_flags(self, monkeypatch) -> None:
        """CREATE_NO_WINDOW ИГНОРИРУЕТСЯ вместе с DETACHED_PROCESS (док Win32).

        Комбинация создавала ложное ощущение, будто окно подавлено именно ею.
        """
        cap = _capture_spawn(monkeypatch)
        monkeypatch.setattr(main_mod.sys, "platform", "win32")
        monkeypatch.setattr(main_mod.Path, "exists", lambda self: True)

        main_mod._spawn_background_upgrade(delay=0, version="1.2.3")
        flags = cap["read"]()["kw"]["creationflags"]
        assert flags == 0x00000008, hex(flags)

    def test_launcher_prefers_windowless_python(self, monkeypatch) -> None:
        monkeypatch.setattr(main_mod.sys, "platform", "win32")
        monkeypatch.setattr(main_mod.sys, "base_prefix", "/base")
        monkeypatch.setattr(main_mod.Path, "exists", lambda self: True)

        assert main_mod._upgrade_launcher().endswith("pythonw.exe")


def test_upgrade_chain_is_single_command_without_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Версия неизвестна — фолбэку неоткуда взяться, цепочка из одной команды."""
    monkeypatch.setattr(main_mod.sys, "executable", "/x/uv/tools/skillery-cli/bin/python")
    monkeypatch.setattr("shutil.which", lambda name: name if name == "uv" else None)

    assert main_mod._upgrade_commands() == [
        ["uv", "tool", "install", "--force", "--refresh", "skillery-cli"]
    ]


def test_cmd_upgrade_windows_spawns_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """На Windows `upgrade` НЕ бежит синхронно (launcher залочен) → фон + return."""
    monkeypatch.setattr("skillery_cli.__version__", "1.0.0")
    monkeypatch.setattr(main_mod, "_fetch_latest_pypi_version", lambda pkg, **k: "2.0.0")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: ClientConfig(base_url="x")))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod.sys, "platform", "win32")
    spawned = {"n": 0}
    monkeypatch.setattr(
        main_mod, "_spawn_background_upgrade",
        lambda *a, **k: (spawned.__setitem__("n", spawned["n"] + 1) or True),
    )
    ran_sync = {"n": 0}
    monkeypatch.setattr("subprocess.run", lambda *a, **k: ran_sync.__setitem__("n", 1))

    main_mod.cmd_upgrade(check=False)
    assert spawned["n"] == 1  # ушло в фон
    assert ran_sync["n"] == 0  # синхронно НЕ запускали (launcher залочен)  # detached, без вывода


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
