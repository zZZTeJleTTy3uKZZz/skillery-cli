"""#1249 — одноимённый бинарь в PATH перекрывает shim навыка.

Реальный инцидент: ``where atlas`` отдаёт сначала ``~/.local/bin/atlas.exe``
(личная pipx-установка), и только потом ``~/.skillery/bin/atlas.cmd``. Вызов
``atlas`` уходит мимо навыка И мимо учёта — МОЛЧА. Проверяем именно это:
подкладываем одноимённый исполняемый файл в каталог, стоящий в PATH РАНЬШЕ
каталога шимов, и требуем, чтобы диагностика его увидела и назвала победителя.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from skillery_cli.core import shim_collisions as sc


# --------------------------------------------------------------------------
#  фикстуры реальной коллизии
# --------------------------------------------------------------------------
def _make_executable(directory: Path, name: str) -> Path:
    """Настоящий исполняемый файл с именем ``name`` (win: .cmd, posix: +x)."""
    directory.mkdir(parents=True, exist_ok=True)
    if sc.IS_WINDOWS:
        path = directory / f"{name}.cmd"
        path.write_text("@echo off\r\necho foreign\r\n", encoding="utf-8", newline="")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture()
def collision_env(tmp_path: Path) -> dict:
    """Каталог шимов с командой ``atlas`` + чужой ``atlas`` в другом каталоге."""
    bin_dir = tmp_path / "skillery" / "bin"
    foreign = tmp_path / "local" / "bin"
    shim = _make_executable(bin_dir, "atlas")
    intruder = _make_executable(foreign, "atlas")
    return {
        "bin_dir": bin_dir,
        "foreign": foreign,
        "shim": shim,
        "intruder": intruder,
        "commands": [("atlas", "atlas")],
    }


def _path(*dirs: Path) -> str:
    return os.pathsep.join(str(d) for d in dirs)


# --------------------------------------------------------------------------
#  сам детект — реальная коллизия
# --------------------------------------------------------------------------
def test_detects_real_collision_when_foreign_dir_is_first(collision_env: dict) -> None:
    """Чужой каталог раньше нашего → коллизия с точным именем победителя."""
    found = sc.detect(
        commands=collision_env["commands"],
        bin_dir=collision_env["bin_dir"],
        path_value=_path(collision_env["foreign"], collision_env["bin_dir"]),
    )
    assert len(found) == 1
    c = found[0]
    assert c.command == "atlas"
    assert c.skill_slug == "atlas"
    assert Path(c.winner) == collision_env["intruder"]
    assert Path(c.winner_dir) == collision_env["foreign"]
    assert Path(c.shim or "") == collision_env["shim"]


def test_no_collision_when_store_dir_is_first(collision_env: dict) -> None:
    """Наш каталог раньше — shim выигрывает, предупреждать не о чем."""
    found = sc.detect(
        commands=collision_env["commands"],
        bin_dir=collision_env["bin_dir"],
        path_value=_path(collision_env["bin_dir"], collision_env["foreign"]),
    )
    assert found == []


def test_collision_when_store_dir_absent_from_path(collision_env: dict) -> None:
    """Каталога шимов в PATH нет вовсе → выигрывает чужой, это тоже коллизия."""
    found = sc.detect(
        commands=collision_env["commands"],
        bin_dir=collision_env["bin_dir"],
        path_value=_path(collision_env["foreign"]),
    )
    assert len(found) == 1
    assert Path(found[0].winner) == collision_env["intruder"]


def test_no_collision_without_foreign_binary(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    other = tmp_path / "other"
    other.mkdir()
    _make_executable(bin_dir, "atlas")
    found = sc.detect(
        commands=[("atlas", "atlas")],
        bin_dir=bin_dir,
        path_value=_path(other, bin_dir),
    )
    assert found == []


def test_only_first_winner_reported(tmp_path: Path) -> None:
    """Победитель ровно один — тот, до кого дойдёт очередь первым."""
    bin_dir = tmp_path / "bin"
    first = tmp_path / "a"
    second = tmp_path / "b"
    _make_executable(bin_dir, "atlas")
    winner = _make_executable(first, "atlas")
    _make_executable(second, "atlas")
    found = sc.detect(
        commands=[("atlas", "atlas")],
        bin_dir=bin_dir,
        path_value=_path(first, second, bin_dir),
    )
    assert len(found) == 1
    assert Path(found[0].winner) == winner


def test_duplicate_store_dir_entry_is_not_a_collision(collision_env: dict) -> None:
    """Наш же каталог, продублированный в PATH, коллизией не считается."""
    found = sc.detect(
        commands=collision_env["commands"],
        bin_dir=collision_env["bin_dir"],
        path_value=_path(collision_env["bin_dir"], collision_env["bin_dir"]),
    )
    assert found == []


# --------------------------------------------------------------------------
#  устойчивость: диагностика не имеет права бросать
# --------------------------------------------------------------------------
def test_detect_never_raises_on_broken_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> list[tuple[str, str]]:
        raise RuntimeError("стор не читается")

    monkeypatch.setattr(sc, "_installed_commands", _boom)
    assert sc.detect(bin_dir=Path("/nope"), path_value="") == []


def test_detect_survives_unreadable_path_entry(collision_env: dict) -> None:
    """Мусорная запись в PATH не мешает найти настоящую коллизию."""
    found = sc.detect(
        commands=collision_env["commands"],
        bin_dir=collision_env["bin_dir"],
        path_value=_path(
            Path("Z:/does/not/exist"), collision_env["foreign"], collision_env["bin_dir"]
        ),
    )
    assert len(found) == 1


def test_detect_empty_when_no_clis_installed(tmp_path: Path) -> None:
    assert sc.detect(commands=[], bin_dir=tmp_path, path_value="") == []


# --------------------------------------------------------------------------
#  текст пользователю
# --------------------------------------------------------------------------
def test_describe_names_winner_slug_and_recipe(collision_env: dict) -> None:
    c = sc.detect(
        commands=collision_env["commands"],
        bin_dir=collision_env["bin_dir"],
        path_value=_path(collision_env["foreign"], collision_env["bin_dir"]),
    )[0]
    text = sc.describe(c)
    assert "atlas" in text
    assert str(collision_env["intruder"]) in text
    assert "НЕ учитывается" in text
    assert "skillery doctor --fix-path-order" in text
    assert "skillery run atlas" in text


def test_summary_lists_all_and_is_empty_without_collisions() -> None:
    assert sc.summary([]) == ""
    c = sc.Collision(
        command="atlas", skill_slug="atlas", winner="/x/atlas", winner_dir="/x"
    )
    text = sc.summary([c])
    assert "atlas" in text and "/x/atlas" in text
    assert "skillery doctor --fix-path-order" in text


# --------------------------------------------------------------------------
#  fix_path_order — обратимость обязательна
# --------------------------------------------------------------------------
@pytest.fixture()
def fake_win_path(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Подменить чтение/запись User PATH у кита (без реального реестра)."""
    from skillkit import path_store

    state = {"value": ""}
    monkeypatch.setattr(sc, "IS_WINDOWS", True)
    monkeypatch.setattr(
        path_store, "_win_read_user_path", lambda: state["value"], raising=False
    )
    monkeypatch.setattr(
        path_store,
        "_win_write_user_path",
        lambda v: state.__setitem__("value", v),
        raising=False,
    )
    return state


def test_fix_path_order_prepends_and_backs_up(
    tmp_path: Path, fake_win_path: dict
) -> None:
    bin_dir = tmp_path / "skillery" / "bin"
    bin_dir.mkdir(parents=True)
    foreign = tmp_path / "local" / "bin"
    fake_win_path["value"] = f"{foreign};{bin_dir}"

    res = sc.fix_path_order(bin_dir=bin_dir)

    assert res["status"] == "reordered"
    # наш каталог теперь первый, чужой на месте — его не выкинули
    parts = fake_win_path["value"].split(";")
    assert Path(parts[0]) == bin_dir
    assert str(foreign) in parts
    # обратимость: прежнее значение лежит в бэкапе
    backup = Path(res["backup"])
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == f"{foreign};{bin_dir}"


def test_fix_path_order_idempotent(tmp_path: Path, fake_win_path: dict) -> None:
    bin_dir = tmp_path / "skillery" / "bin"
    bin_dir.mkdir(parents=True)
    fake_win_path["value"] = f"{bin_dir};C:\\other"
    assert sc.fix_path_order(bin_dir=bin_dir)["status"] == "already"


def test_fix_path_order_manual_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc, "IS_WINDOWS", False)
    res = sc.fix_path_order(bin_dir=tmp_path / "bin")
    assert res["status"] == "manual-needed"
    assert "export PATH" in res["instruction"]


def test_fix_path_order_manual_when_registry_write_fails(
    tmp_path: Path, fake_win_path: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skillkit import path_store

    bin_dir = tmp_path / "skillery" / "bin"
    bin_dir.mkdir(parents=True)
    fake_win_path["value"] = "C:\\other"

    def _boom(_value: str) -> None:
        raise OSError("нет доступа к реестру")

    monkeypatch.setattr(path_store, "_win_write_user_path", _boom, raising=False)
    res = sc.fix_path_order(bin_dir=bin_dir)
    assert res["status"] == "manual-needed"
    assert res["instruction"]
    # бэкап всё равно снят — пользователю есть к чему откатиться
    assert Path(res["backup"]).is_file()


# --------------------------------------------------------------------------
#  точки, где пользователь ЭТО видит: doctor / status / установка навыка
# --------------------------------------------------------------------------
def test_doctor_probe_warns_on_collision(
    collision_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skillery doctor` обязан показать коллизию, а не молчать (#1249)."""
    from skillery_cli.commands import doctor as doc

    monkeypatch.setenv(
        "PATH", _path(collision_env["foreign"], collision_env["bin_dir"])
    )
    monkeypatch.setattr(
        sc, "_installed_commands", lambda: [("atlas", "atlas")]
    )
    monkeypatch.setattr("skillkit.path_store.bin_dir", lambda: collision_env["bin_dir"])

    res = doc._probe_shim_collisions()
    assert res.level == "warn"
    assert "atlas" in res.detail
    assert "skillery doctor --fix-path-order" in res.detail


def test_doctor_probe_passes_without_collision(
    collision_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skillery_cli.commands import doctor as doc

    monkeypatch.setenv(
        "PATH", _path(collision_env["bin_dir"], collision_env["foreign"])
    )
    monkeypatch.setattr(sc, "_installed_commands", lambda: [("atlas", "atlas")])
    monkeypatch.setattr("skillkit.path_store.bin_dir", lambda: collision_env["bin_dir"])
    assert doc._probe_shim_collisions().level == "pass"


def test_doctor_probe_included_in_run_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка должна быть в общем прогоне, а не жить отдельной командой."""
    from skillery_cli.commands import doctor as doc
    from skillery_cli.config import ClientConfig

    monkeypatch.setattr(
        doc, "_probe_shim_collisions", lambda: doc.warn("PATH-коллизии", "atlas")
    )
    names = {r.name for r in doc.run_checks(ClientConfig(base_url="http://x"))}
    assert "PATH-коллизии" in names


def test_install_report_warns_on_collision(
    collision_env: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Установка tooling-навыка сразу говорит о перехвате его команды."""
    from skillery_cli import __main__ as main_mod

    monkeypatch.setenv(
        "PATH", _path(collision_env["foreign"], collision_env["bin_dir"])
    )
    monkeypatch.setattr("skillkit.path_store.bin_dir", lambda: collision_env["bin_dir"])

    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_mod,
        "emit_message",
        lambda text, level="info", **kw: messages.append((level, text)),
    )
    main_mod._report_tooling(
        {"cli": [{"command_name": "atlas", "status": "installed", "path": {}}]}
    )
    warns = [t for lvl, t in messages if lvl == "warn"]
    assert any("перехвачена" in t and "atlas" in t for t in warns)


def test_install_report_silent_without_collision(
    collision_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skillery_cli import __main__ as main_mod

    monkeypatch.setenv(
        "PATH", _path(collision_env["bin_dir"], collision_env["foreign"])
    )
    monkeypatch.setattr("skillkit.path_store.bin_dir", lambda: collision_env["bin_dir"])
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_mod,
        "emit_message",
        lambda text, level="info", **kw: messages.append((level, text)),
    )
    main_mod._report_tooling(
        {"cli": [{"command_name": "atlas", "status": "installed", "path": {}}]}
    )
    assert [t for lvl, t in messages if lvl == "warn"] == []


def test_install_report_survives_broken_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сломанная диагностика — предупреждение, а не падение установки."""
    from skillery_cli import __main__ as main_mod

    def _boom(*_a: object, **_k: object) -> list:
        raise RuntimeError("детект сломан")

    monkeypatch.setattr(sc, "detect_for_command", _boom)
    monkeypatch.setattr(main_mod, "emit_message", lambda *a, **k: None)
    main_mod._report_tooling(
        {"cli": [{"command_name": "atlas", "status": "installed", "path": {}}]}
    )
