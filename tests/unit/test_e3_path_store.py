"""E3 фаза 1 — core/path_store: локальный стор CLI-шимов навыков + PATH.

Цель: CLI установленного навыка («command_name» из E6 ``[[cli]]``) должен
зваться из ЛЮБОЙ директории. Для этого path_store:
- кладёт исполняемый shim в bin-каталог стора (``~/.skills-hub/bin``);
- умеет убрать shim (disable/remove навыка);
- кроссплатформенно гарантирует bin-каталог в PATH (idempotent);
- перечисляет, какие CLI установлены.

Тесты мокают платформу/окружение, чтобы покрыть win/mac/linux ветки на любой ОС.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from skills_hub_cli.core import path_store


@pytest.fixture()
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный bin-каталог через env-override SKILLS_HUB_BIN_DIR."""
    d = tmp_path / "skills-hub" / "bin"
    monkeypatch.setenv("SKILLS_HUB_BIN_DIR", str(d))
    return d


# --------------------------------------------------------------------------
#  bin_dir() — деривация пути
# --------------------------------------------------------------------------
def test_bin_dir_default_is_store_parent_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """По умолчанию bin = effective_store_dir().parent / 'bin'."""
    monkeypatch.delenv("SKILLS_HUB_BIN_DIR", raising=False)
    monkeypatch.setenv("SKILLS_HUB_STORE_DIR", str(Path.home() / ".skills-hub" / "store"))
    expected = (Path.home() / ".skills-hub" / "store").parent / "bin"
    assert path_store.bin_dir() == expected


def test_bin_dir_env_override(bin_dir: Path) -> None:
    assert path_store.bin_dir() == bin_dir


# --------------------------------------------------------------------------
#  add_cli — Windows ветка (.cmd shim)
# --------------------------------------------------------------------------
def test_add_cli_windows_writes_cmd_shim(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    shim = path_store.add_cli("mytool", "python -m mytool.cli", skill_slug="demo")
    assert shim.suffix == ".cmd"
    assert shim.name == "mytool.cmd"
    assert shim.exists()
    body = shim.read_text(encoding="utf-8")
    # .cmd-shim: батник, прокидывает все аргументы (%*) в энтрипоинт.
    assert "@echo off" in body.lower()
    assert "python -m mytool.cli" in body
    assert "%*" in body


def test_add_cli_windows_venv_entrypoint_exe(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Энтрипоинт-путь к venv-exe → .cmd вызывает его напрямую (в кавычках)."""
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    exe = tmp_path / "venv" / "Scripts" / "mytool.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("binary", encoding="utf-8")
    shim = path_store.add_cli("mytool", str(exe), skill_slug="demo")
    body = shim.read_text(encoding="utf-8")
    assert f'"{exe}"' in body
    assert "%*" in body


# --------------------------------------------------------------------------
#  add_cli — POSIX ветка (shebang-скрипт + chmod +x)
# --------------------------------------------------------------------------
def test_add_cli_posix_writes_executable_shebang(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", False)
    shim = path_store.add_cli("mytool", "python -m mytool.cli", skill_slug="demo")
    assert shim.name == "mytool"  # без расширения на POSIX
    assert shim.exists()
    body = shim.read_text(encoding="utf-8")
    assert body.startswith("#!")  # shebang
    assert "python -m mytool.cli" in body
    assert '"$@"' in body  # прокидывает аргументы
    # chmod +x — биты исполнения выставлены (на NTFS x-бит не представим →
    # проверяем только на POSIX-хосте, где он осмыслен).
    if sys.platform != "win32":
        mode = shim.stat().st_mode
        assert mode & stat.S_IXUSR


def test_add_cli_posix_symlinks_existing_executable(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """POSIX: если энтрипоинт — существующий исполняемый файл, ставим symlink."""
    if sys.platform == "win32":
        pytest.skip("symlink-ветка проверяется на POSIX-хосте")
    monkeypatch.setattr(path_store, "IS_WINDOWS", False)
    real = tmp_path / "bin-real" / "mytool"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    real.chmod(0o755)
    shim = path_store.add_cli("mytool", str(real), skill_slug="demo")
    assert shim.is_symlink()
    assert Path(os.readlink(shim)) == real


def test_add_cli_idempotent_overwrite(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторный add_cli того же command → перезапись, не дубль/ошибка."""
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    path_store.add_cli("mytool", "python -m mytool.cli", skill_slug="demo")
    shim = path_store.add_cli("mytool", "python -m mytool.cli2", skill_slug="demo")
    body = shim.read_text(encoding="utf-8")
    assert "mytool.cli2" in body
    assert len(path_store.list_clis()) == 1


def test_add_cli_records_skill_slug(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add_cli сохраняет привязку к навыку (для list/remove по навыку)."""
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    path_store.add_cli("mytool", "python -m mytool.cli", skill_slug="demo")
    clis = path_store.list_clis()
    assert clis[0]["command_name"] == "mytool"
    assert clis[0]["skill_slug"] == "demo"


# --------------------------------------------------------------------------
#  remove_cli
# --------------------------------------------------------------------------
def test_remove_cli_windows(bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    path_store.add_cli("mytool", "python -m mytool.cli", skill_slug="demo")
    assert path_store.remove_cli("mytool") is True
    assert path_store.list_clis() == []
    # повторное удаление — no-op (False), без исключения.
    assert path_store.remove_cli("mytool") is False


def test_remove_cli_posix(bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", False)
    path_store.add_cli("mytool", "python -m mytool.cli", skill_slug="demo")
    assert path_store.remove_cli("mytool") is True
    assert not (bin_dir / "mytool").exists()
    assert path_store.list_clis() == []


def test_remove_cli_also_drops_sidecar(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    path_store.add_cli("mytool", "python -m mytool.cli", skill_slug="demo")
    path_store.remove_cli("mytool")
    # ни шима, ни sidecar-метаданных не осталось → каталог пуст от мусора.
    leftover = [p.name for p in bin_dir.iterdir()] if bin_dir.exists() else []
    assert "mytool.cmd" not in leftover
    assert all(not n.startswith("mytool.") for n in leftover)


# --------------------------------------------------------------------------
#  list_clis
# --------------------------------------------------------------------------
def test_list_clis_empty(bin_dir: Path) -> None:
    assert path_store.list_clis() == []


def test_list_clis_multiple_sorted(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    path_store.add_cli("btool", "python -m b", skill_slug="b-skill")
    path_store.add_cli("atool", "python -m a", skill_slug="a-skill")
    clis = path_store.list_clis()
    assert [c["command_name"] for c in clis] == ["atool", "btool"]


# --------------------------------------------------------------------------
#  ensure_on_path — POSIX
# --------------------------------------------------------------------------
def test_ensure_on_path_already_present_posix(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", False)
    bin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin")
    res = path_store.ensure_on_path()
    assert res["status"] == "already"


def test_ensure_on_path_appends_to_rc_posix(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """POSIX: bin не в PATH → дописываем export-строку в rc-файл (status=added)."""
    monkeypatch.setattr(path_store, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", "/usr/bin")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    rc = fake_home / ".profile"
    rc.write_text("# existing\n", encoding="utf-8")
    monkeypatch.setattr(path_store, "_posix_rc_file", lambda: rc)
    res = path_store.ensure_on_path()
    assert res["status"] in ("added", "manual-needed")
    if res["status"] == "added":
        body = rc.read_text(encoding="utf-8")
        assert str(bin_dir) in body
        assert "export PATH" in body


def test_ensure_on_path_idempotent_posix(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Повторный ensure_on_path НЕ дублирует export-строку в rc."""
    monkeypatch.setattr(path_store, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", "/usr/bin")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    rc = fake_home / ".profile"
    rc.write_text("", encoding="utf-8")
    monkeypatch.setattr(path_store, "_posix_rc_file", lambda: rc)
    path_store.ensure_on_path()
    path_store.ensure_on_path()
    body = rc.read_text(encoding="utf-8")
    # Строка с bin-каталогом встречается максимум один раз.
    assert body.count(str(bin_dir)) <= 1


def test_ensure_on_path_manual_when_no_rc(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Если rc-файл недоступен на запись → manual-needed + инструкция."""
    monkeypatch.setattr(path_store, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", "/usr/bin")

    def _boom() -> Path:
        raise OSError("no writable rc")

    monkeypatch.setattr(path_store, "_posix_rc_file", _boom)
    res = path_store.ensure_on_path()
    assert res["status"] == "manual-needed"
    assert "export PATH" in res["instruction"]
    assert str(bin_dir) in res["instruction"]


# --------------------------------------------------------------------------
#  ensure_on_path — Windows (registry HKCU, без admin)
# --------------------------------------------------------------------------
def test_ensure_on_path_already_present_windows(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    # читатель User PATH вернёт каталог как уже присутствующий.
    monkeypatch.setattr(
        path_store, "_win_read_user_path", lambda: str(bin_dir) + ";C:\\Windows"
    )
    set_calls: list[str] = []
    monkeypatch.setattr(
        path_store, "_win_write_user_path", lambda v: set_calls.append(v)
    )
    res = path_store.ensure_on_path()
    assert res["status"] == "already"
    assert set_calls == []  # ничего не писали


def test_ensure_on_path_adds_to_user_path_windows(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(path_store, "_win_read_user_path", lambda: "C:\\Windows")
    written: list[str] = []
    monkeypatch.setattr(
        path_store, "_win_write_user_path", lambda v: written.append(v)
    )
    res = path_store.ensure_on_path()
    assert res["status"] == "added"
    assert len(written) == 1
    assert str(bin_dir) in written[0]
    assert "C:\\Windows" in written[0]  # старое значение сохранено


def test_ensure_on_path_idempotent_windows(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Второй ensure (после того как PATH уже содержит bin) → already, без записи."""
    monkeypatch.setattr(path_store, "IS_WINDOWS", True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    current = {"v": "C:\\Windows"}
    monkeypatch.setattr(path_store, "_win_read_user_path", lambda: current["v"])
    monkeypatch.setattr(
        path_store, "_win_write_user_path", lambda v: current.__setitem__("v", v)
    )
    r1 = path_store.ensure_on_path()
    r2 = path_store.ensure_on_path()
    assert r1["status"] == "added"
    assert r2["status"] == "already"
    # bin-каталог в значении ровно один раз.
    assert current["v"].count(str(bin_dir)) == 1
