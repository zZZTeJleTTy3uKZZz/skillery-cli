"""SK-5: install-путь пишет структурный аудит в cli.log/daemon.log.

Раньше cli.log/daemon.log оставались 0 байт (уровень ERROR отбрасывал шаги
установки) → тихий пропуск установки CLI-пакета (SK-2) диагностировался вручную.
Здесь закреплено: аудит установки пишется ВСЕГДА (INFO), а провал команды — WARNING.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from skillery_cli import __main__ as m
from skillery_cli.core import logging_setup as ls


@pytest.fixture(autouse=True)
def _reset_install_logger():
    """Снять file-хендлеры аудит-логгера между тестами (tmp-файлы разные)."""
    lg = logging.getLogger("skillery.install")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    yield
    for h in list(lg.handlers):
        lg.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def _records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_install_logger_writes_info_despite_error_root(tmp_path, monkeypatch) -> None:
    """Аудит пишет INFO, даже когда общий cli.log-логгер на уровне ERROR."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    # общий логгер на ERROR (как дефолт) — не должен глушить аудит install
    ls.configure_logging("error", filename="cli.log")

    lg = ls.install_logger("cli.log")
    lg.info("шаг установки", extra={"context": {"step": "materialize"}})

    recs = _records(tmp_path / "cli.log")
    assert any(r["message"] == "шаг установки" and r["level"] == "INFO" for r in recs)


def test_install_logger_daemon_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    ls.install_logger("daemon.log").info("демон-установка")
    assert any(r["message"] == "демон-установка" for r in _records(tmp_path / "daemon.log"))


def test_apply_tooling_audits_installed_cli_package(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    store = tmp_path / "store" / "foo"
    (store / ".pkgsrc").mkdir(parents=True)
    (store / ".pkgsrc" / "pyproject.toml").write_text(
        '[project]\nname="foo-cli"\n[project.scripts]\nfoo="foo.cli:main"\n', encoding="utf-8"
    )
    result = type("R", (), {"store_dir": store})()
    manifest = {"cli": [{"command_name": "foo", "entrypoint": "foo.cli:main"}]}

    monkeypatch.setattr(
        m.cli_package_install, "install_cli_package",
        lambda *a, **k: {"status": "installed", "package": "foo-cli",
                         "commands": [{"name": "foo", "on_path": True, "ok": True}], "reason": ""},
    )
    monkeypatch.setattr(m, "_remove_stale_shim", lambda n: None)
    monkeypatch.setattr(m.tooling_install, "apply_tooling_artifacts", lambda *a, **k: {"cli": [], "mcp": [], "deps": {}})

    m._apply_tooling(result, manifest, agent_target=object(), project=None, log_file="cli.log")

    recs = _records(tmp_path / "cli.log")
    installed = [r for r in recs if r["context"].get("step") == "cli_package"]
    assert installed and installed[0]["level"] == "INFO"
    assert installed[0]["context"]["package"] == "foo-cli"
    assert installed[0]["context"]["status"] == "installed"


def test_apply_tooling_warns_on_failed_cli_package(tmp_path, monkeypatch) -> None:
    """Провал установки команды → WARNING (то, чего не хватало для диагностики)."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    store = tmp_path / "store" / "foo"
    (store / ".pkgsrc").mkdir(parents=True)
    (store / ".pkgsrc" / "pyproject.toml").write_text(
        '[project]\nname="foo-cli"\n[project.scripts]\nfoo="foo.cli:main"\n', encoding="utf-8"
    )
    result = type("R", (), {"store_dir": store})()
    manifest = {"cli": [{"command_name": "foo", "entrypoint": "foo.cli:main"}]}

    monkeypatch.setattr(
        m.cli_package_install, "install_cli_package",
        lambda *a, **k: {"status": "error", "package": "foo-cli", "commands": [],
                         "reason": "uv tool install вернул код 1"},
    )
    monkeypatch.setattr(m.tooling_install, "apply_tooling_artifacts", lambda *a, **k: {"cli": [], "mcp": [], "deps": {}})

    m._apply_tooling(result, manifest, agent_target=object(), project=None, log_file="cli.log")

    recs = _records(tmp_path / "cli.log")
    failed = [r for r in recs if r["context"].get("step") == "cli_package"]
    assert failed and failed[0]["level"] == "WARNING"
    assert "код 1" in failed[0]["context"]["reason"]
