"""Установка CLI-пакета навыка через `uv tool install` (как install-скрипты навыков).

Приватный CLI-навык — монорепо: доки в skills/<name>/, пакет в КОРНЕ. Раньше корень
выкидывался и пакет не ставился → `tg` падал ModuleNotFoundError. Здесь закреплено,
что из корня снапшота ставится `uv tool install --force <корень>`, self-check мягкий,
а снятие идёт `uv tool uninstall <project.name>`.
"""
from __future__ import annotations

from pathlib import Path

from skillery_cli.core import cli_package_install as cpi

_PYPROJECT = """\
[project]
name = "telegram-content-cli"
version = "0.1.9"

[project.scripts]
tg = "telegram_content.cli:main"
"""


def _mkpkg(root: Path) -> Path:
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "src").mkdir()
    return root


class TestReadPyproject:
    def test_reads_name_and_scripts(self, tmp_path: Path) -> None:
        _mkpkg(tmp_path)
        info = cpi.read_pyproject_cli(tmp_path)
        assert info == {
            "name": "telegram-content-cli",
            "scripts": {"tg": "telegram_content.cli:main"},
        }

    def test_none_without_pyproject(self, tmp_path: Path) -> None:
        assert cpi.read_pyproject_cli(tmp_path) is None

    def test_none_without_project_name(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
        assert cpi.read_pyproject_cli(tmp_path) is None


class TestInstall:
    def test_skipped_when_root_has_no_package(self, tmp_path: Path) -> None:
        r = cpi.install_cli_package(tmp_path, runner=lambda cmd: 0, which=lambda n: "uv")
        assert r["status"] == "skipped" and r["package"] is None

    def test_error_when_uv_missing(self, tmp_path: Path) -> None:
        _mkpkg(tmp_path)
        r = cpi.install_cli_package(tmp_path, which=lambda n: None, runner=lambda c: 0)
        assert r["status"] == "error" and "uv" in r["reason"]
        assert r["package"] == "telegram-content-cli"

    def test_installed_runs_uv_tool_install_force_root(self, tmp_path: Path) -> None:
        _mkpkg(tmp_path)
        calls: list[list[str]] = []

        def run(cmd: list[str]) -> int:
            calls.append(cmd)
            return 0

        r = cpi.install_cli_package(
            tmp_path, uv_path="uv", which=lambda n: "/bin/" + n, runner=run
        )
        assert r["status"] == "installed" and r["package"] == "telegram-content-cli"
        # первая команда — ровно `uv tool install --force <корень>` (эталон install.py)
        assert calls[0] == ["uv", "tool", "install", "--force", str(tmp_path)]
        # self-check команды `tg` (на PATH) — запускалась
        assert r["commands"] == [{"name": "tg", "on_path": True, "ok": True}]

    def test_error_when_uv_tool_install_fails(self, tmp_path: Path) -> None:
        _mkpkg(tmp_path)
        r = cpi.install_cli_package(
            tmp_path, uv_path="uv", which=lambda n: "uv", runner=lambda c: 3
        )
        assert r["status"] == "error" and "код 3" in r["reason"]

    def test_installed_but_not_on_path_is_not_failure(self, tmp_path: Path) -> None:
        """Команда не на PATH ≠ провал установки: PATH просто не обновился."""
        _mkpkg(tmp_path)

        # uv есть, но искомой команды `tg` на PATH ещё нет.
        def which(n: str) -> str | None:
            return "uv" if n == "uv" else None

        r = cpi.install_cli_package(tmp_path, which=which, runner=lambda c: 0)
        assert r["status"] == "installed"
        assert r["commands"] == [{"name": "tg", "on_path": False, "ok": False}]

    def test_explicit_command_names_override_scripts(self, tmp_path: Path) -> None:
        _mkpkg(tmp_path)
        r = cpi.install_cli_package(
            tmp_path, command_names=["tg"], which=lambda n: "/b/" + n,
            runner=lambda c: 0,
        )
        assert [c["name"] for c in r["commands"]] == ["tg"]


class TestUninstall:
    def test_runs_uv_tool_uninstall(self) -> None:
        calls: list[list[str]] = []

        def run(cmd: list[str]) -> int:
            calls.append(cmd)
            return 0

        ok = cpi.uninstall_cli_package(
            "telegram-content-cli", uv_path="uv", which=lambda n: "uv", runner=run
        )
        assert ok is True
        assert calls == [["uv", "tool", "uninstall", "telegram-content-cli"]]

    def test_false_without_name_or_uv(self) -> None:
        assert cpi.uninstall_cli_package("", runner=lambda c: 0) is False
        assert (
            cpi.uninstall_cli_package("x", which=lambda n: None, runner=lambda c: 0)
            is False
        )
