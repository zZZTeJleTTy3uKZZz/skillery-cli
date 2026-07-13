"""E3 фаза 1 — ``skillery doctor`` (self-check окружения, образец reverse-factory).

Команда always-on (как status): проверяет окружение и печатает pass/warn/fail.
``--strict`` ужесточает warn→fail (для CI). JSON-режим даёт машинную структуру.

Проверки (каждая — отдельный probe, мокается в тесте):
- Python >= 3.11 (fail если ниже);
- менеджер пакетов uv/pip (warn только pip, fail если ничего);
- agent-детект (claude_code/codex/...);
- hub login-статус (pass/warn);
- PATH-стор (bin в PATH? pass/warn);
- clikit доступен (опц., warn если нет).
"""
from __future__ import annotations

import json as _json

import pytest

from skillery_cli import output as out_mod
from skillery_cli.commands import doctor as doc
from skillery_cli.config import ClientConfig


@pytest.fixture()
def all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сделать все probe-функции зелёными (база для точечных мутаций)."""
    monkeypatch.setattr(doc, "_probe_python", lambda: doc.ok("Python >= 3.11", "3.12.0"))
    monkeypatch.setattr(
        doc, "_probe_package_manager", lambda: doc.ok("Менеджер пакетов", "uv")
    )
    monkeypatch.setattr(doc, "_probe_agent", lambda cfg: doc.ok("Агент", "claude_code"))
    monkeypatch.setattr(doc, "_probe_login", lambda cfg: doc.ok("Hub login", "user@x"))
    monkeypatch.setattr(doc, "_probe_path_store", lambda: doc.ok("PATH-стор", "/bin"))
    monkeypatch.setattr(doc, "_probe_clikit", lambda: doc.ok("clikit", "0.1"))
    monkeypatch.setattr(doc, "_probe_config", lambda: doc.ok("config", "валиден"))
    monkeypatch.setattr(doc, "_probe_cli_version", lambda: doc.ok("cli-version", "ok"))


def _make_cfg() -> ClientConfig:
    return ClientConfig(base_url="http://localhost:8000")


# --------------------------------------------------------------------------
#  Result helpers / уровни
# --------------------------------------------------------------------------
def test_result_levels() -> None:
    assert doc.ok("x").level == "pass"
    assert doc.warn("x").level == "warn"
    assert doc.fail("x").level == "fail"


# --------------------------------------------------------------------------
#  run_checks — агрегатор
# --------------------------------------------------------------------------
def test_run_checks_all_green(all_green: None) -> None:
    results = doc.run_checks(_make_cfg())
    assert all(r.level == "pass" for r in results)
    # покрыты все 6 направлений.
    names = {r.name for r in results}
    assert len(names) >= 6


def test_run_checks_collects_each_level(
    all_green: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doc, "_probe_login", lambda cfg: doc.warn("Hub login", "нет"))
    monkeypatch.setattr(
        doc, "_probe_python", lambda: doc.fail("Python >= 3.11", "3.10")
    )
    results = doc.run_checks(_make_cfg())
    levels = {r.name: r.level for r in results}
    assert levels["Python >= 3.11"] == "fail"
    assert levels["Hub login"] == "warn"


# --------------------------------------------------------------------------
#  Отдельные probe — pass/warn/fail
# --------------------------------------------------------------------------
def test_probe_python_pass() -> None:
    r = doc._probe_python()
    assert r.level == "pass"  # тесты идут на 3.11+


def test_probe_package_manager_uv_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    r = doc._probe_package_manager()
    assert r.level == "pass"
    assert "uv" in r.detail


def test_probe_package_manager_pip_only_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc.shutil, "which", lambda name: None)
    monkeypatch.setattr(doc, "_pip_available", lambda: True)
    r = doc._probe_package_manager()
    assert r.level == "warn"


def test_probe_package_manager_none_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc.shutil, "which", lambda name: None)
    monkeypatch.setattr(doc, "_pip_available", lambda: False)
    r = doc._probe_package_manager()
    assert r.level == "fail"


def test_probe_agent_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc, "detect_agent", lambda: "codex")
    # таргет codex «существует».
    class _T:
        name = "codex"

        def exists(self) -> bool:
            return True

    monkeypatch.setattr(doc, "get_target", lambda name: _T())
    r = doc._probe_agent(_make_cfg())
    assert r.level == "pass"
    assert "codex" in r.detail


def test_probe_agent_none_installed_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc, "detect_agent", lambda: "claude_code")

    class _T:
        name = "claude_code"

        def exists(self) -> bool:
            return False

    monkeypatch.setattr(doc, "get_target", lambda name: _T())
    r = doc._probe_agent(_make_cfg())
    assert r.level == "warn"  # ни один агент не установлен → fallback, мягко


def test_probe_login_pass() -> None:
    cfg = ClientConfig(base_url="http://x", user_email="u@x", permissions=["skill.read"])
    assert doc._probe_login(cfg).level == "pass"


def test_probe_login_warn_when_anon() -> None:
    cfg = ClientConfig(base_url="http://x")
    assert doc._probe_login(cfg).level == "warn"


def test_probe_path_store_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc.path_store, "_already_on_path", lambda target: True)
    r = doc._probe_path_store()
    assert r.level == "pass"


def test_probe_path_store_warn_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc.path_store, "_already_on_path", lambda target: False)
    r = doc._probe_path_store()
    assert r.level == "warn"
    assert "doctor" in r.detail.lower() or "path" in r.detail.lower()


def test_probe_clikit_warn_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc.importlib.util, "find_spec", lambda name: None)
    r = doc._probe_clikit()
    assert r.level == "warn"


# --------------------------------------------------------------------------
#  cmd_doctor — exit-коды + --strict + JSON
# --------------------------------------------------------------------------
def test_cmd_doctor_all_green_exit_zero(
    all_green: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(out_mod, "_mode", "text")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: _make_cfg()))
    # зелёный прогон не вызывает Exit (код 0).
    doc.cmd_doctor(strict=False)


def test_cmd_doctor_fail_exits_nonzero(
    all_green: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    monkeypatch.setattr(out_mod, "_mode", "text")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: _make_cfg()))
    monkeypatch.setattr(doc, "_probe_python", lambda: doc.fail("Python >= 3.11", "3.9"))
    with pytest.raises(typer.Exit) as exc:
        doc.cmd_doctor(strict=False)
    assert exc.value.exit_code == 1


def test_cmd_doctor_strict_turns_warn_into_fail(
    all_green: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    monkeypatch.setattr(out_mod, "_mode", "text")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: _make_cfg()))
    monkeypatch.setattr(doc, "_probe_login", lambda cfg: doc.warn("Hub login", "нет"))
    # без strict — warn не валит (код 0).
    doc.cmd_doctor(strict=False)
    # со strict — warn → нонзеро.
    with pytest.raises(typer.Exit) as exc:
        doc.cmd_doctor(strict=True)
    assert exc.value.exit_code == 1


def test_cmd_doctor_json_shape(
    all_green: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(out_mod, "_mode", "json")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: _make_cfg()))
    monkeypatch.setattr(doc, "_probe_login", lambda cfg: doc.warn("Hub login", "нет"))
    doc.cmd_doctor(strict=False)
    data = _json.loads(capsys.readouterr().out)
    assert "checks" in data
    assert isinstance(data["checks"], list)
    assert all({"name", "level", "detail"} <= set(c) for c in data["checks"])
    assert "summary" in data
    assert data["summary"]["warn"] >= 1
    assert data["ok"] is True  # warn без strict → окружение пригодно
    assert data["strict"] is False


def test_cmd_doctor_json_strict_ok_false(
    all_green: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import typer

    monkeypatch.setattr(out_mod, "_mode", "json")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: _make_cfg()))
    monkeypatch.setattr(doc, "_probe_login", lambda cfg: doc.warn("Hub login", "нет"))
    with pytest.raises(typer.Exit):
        doc.cmd_doctor(strict=True)
    data = _json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["strict"] is True


def test_cmd_doctor_fix_path_invokes_ensure_and_reports(
    all_green: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--fix-path вызывает ensure_on_path и кладёт его результат в payload."""
    monkeypatch.setattr(out_mod, "_mode", "json")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: _make_cfg()))
    calls: list[bool] = []

    def _fake_ensure() -> dict[str, str]:
        calls.append(True)
        return {"status": "added", "bin_dir": "/home/u/.skillery/bin"}

    monkeypatch.setattr(doc.path_store, "ensure_on_path", _fake_ensure)
    doc.cmd_doctor(strict=False, fix_path=True)
    data = _json.loads(capsys.readouterr().out)
    assert calls == [True]
    assert data["path_fix"]["status"] == "added"


def test_cmd_doctor_no_fix_path_skips_ensure(
    all_green: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Без --fix-path ensure_on_path НЕ вызывается (doctor read-only по умолчанию)."""
    monkeypatch.setattr(out_mod, "_mode", "json")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: _make_cfg()))

    def _boom() -> dict[str, str]:
        raise AssertionError("ensure_on_path не должен вызываться без --fix-path")

    monkeypatch.setattr(doc.path_store, "ensure_on_path", _boom)
    doc.cmd_doctor(strict=False, fix_path=False)
    data = _json.loads(capsys.readouterr().out)
    assert "path_fix" not in data


# --------------------------------------------------------------------------
#  Регистрация always-on
# --------------------------------------------------------------------------
def test_doctor_registered_always_on(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ClientConfig(base_url="http://localhost:8000")  # не залогинен
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "doctor" in names


# --------------------------------------------------------------------------
#  config probe + repair_config (self-heal)
# --------------------------------------------------------------------------
def test_probe_config_flags_schemeless_base_url(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "config.toml"
    p.write_text('base_url = "x"\n', encoding="utf-8")
    monkeypatch.setattr(doc, "_default_config_file", lambda: p, raising=False)
    from skillery_cli import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_default_config_file", lambda: p)
    r = doc._probe_config()
    assert r.level == "fail"
    assert "base_url" in r.detail


def test_repair_config_fixes_schemeless_base_url(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKILLERY_BASE_URL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text('base_url = "x"\n', encoding="utf-8")
    from skillery_cli import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_default_config_file", lambda: p)
    repairs = doc.repair_config()
    assert len(repairs) == 1 and "base_url" in repairs[0]
    assert 'base_url = "https://api.skillery.ru"' in p.read_text(encoding="utf-8")
    # идемпотентно: повторный вызов — уже нечего чинить
    assert doc.repair_config() == []


def test_repair_config_resets_unparseable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "config.toml"
    p.write_text('base_url = "broken\n[[[', encoding="utf-8")  # битый TOML
    from skillery_cli import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_default_config_file", lambda: p)
    repairs = doc.repair_config()
    assert len(repairs) == 1 and "сброшен" in repairs[0]
    assert (tmp_path / "config.toml.bak").exists()  # бэкап
    # новый конфиг парсится
    import tomllib
    tomllib.loads(p.read_text(encoding="utf-8"))
