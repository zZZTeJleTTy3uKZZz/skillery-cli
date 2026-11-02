"""Команды ``skillery product``: install --plan-file, list, remove (#5985).

Проверяется контракт CLI, а не внутренности движка: команда читает план,
пишет/читает ``~/.skillery/products.json``, под ``--json`` отдаёт машинный
результат (stdout — только JSON), а ошибки — ненулевым кодом. Никаких
настоящих uv/claude/установщиков: планы в тестах ссылаются на локальные файлы.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from skillery_cli import output as output_module
from skillery_cli.commands import product as product_mod


@pytest.fixture(autouse=True)
def _json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DSH_HOME", "DSH_PROFILE", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(output_module, "_mode", "json")


def _app() -> typer.Typer:
    root = typer.Typer()
    product_mod.register(root)
    return root


def _plan(tmp_path: Path, *, url: str = "", sha256: str = "") -> Path:
    payload = b"#!/bin/sh\necho demo\n"
    source = tmp_path / "demo-bin"
    source.write_bytes(payload)
    data = {
        "products": [
            {
                "slug": "demo",
                "version": "1.0.0",
                "components": [
                    {
                        "key": "demo-cli",
                        "type": "cli",
                        "version": "1.0.0",
                        "artifact": {
                            "kind": "binary",
                            "os": "linux",
                            "arch": "amd64",
                            "url": url or source.as_uri(),
                            "sha256": sha256 or hashlib.sha256(payload).hexdigest(),
                            "install": {"bin": "demo"},
                        },
                    }
                ],
            }
        ]
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _json_stdout(result) -> dict:  # noqa: ANN001
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.output
    return json.loads(lines[-1])


def test_install_then_list_then_remove(tmp_path: Path) -> None:
    plan_file = _plan(tmp_path)
    runner = CliRunner()

    installed = runner.invoke(_app(), ["product", "install", "--plan-file", str(plan_file)])
    assert installed.exit_code == 0, installed.output
    payload = _json_stdout(installed)
    assert payload["products"][0]["components"][0]["status"] == "installed"

    listed = runner.invoke(_app(), ["product", "list"])
    assert listed.exit_code == 0
    listing = _json_stdout(listed)
    assert listing["count"] == 1
    assert listing["products"][0]["slug"] == "demo"

    removed = runner.invoke(_app(), ["product", "remove", "demo"])
    assert removed.exit_code == 0, removed.output
    assert _json_stdout(removed)["status"] == "removed"
    assert not (Path.home() / ".local" / "bin" / "demo").exists()

    again = runner.invoke(_app(), ["product", "remove", "demo"])
    assert again.exit_code == 1
    assert "не установлен" in again.stderr.lower() or "не установлен" in again.output.lower()


def test_dry_run_does_not_touch_machine(tmp_path: Path) -> None:
    payload = b"#!/bin/sh\n"
    source = tmp_path / "demo-bin"
    source.write_bytes(payload)
    plan = {
        "products": [
            {
                "slug": "demo",
                "components": [
                    {
                        "key": "demo-cli",
                        "type": "cli",
                        "artifact": {
                            "kind": "uv-tool",
                            "os": "any",
                            "arch": "any",
                            "install": {"package": "demo-tool"},
                        },
                    }
                ],
            }
        ]
    }
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")

    result = CliRunner().invoke(
        _app(), ["product", "install", "--plan-file", str(plan_file), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert _json_stdout(result)["products"][0]["components"][0]["status"] == "planned"
    assert not (Path.home() / ".skillery" / "products.json").exists()


def test_install_rejects_broken_plan(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(
        _app(), ["product", "install", "--plan-file", str(broken)]
    )
    assert result.exit_code == 1
    assert "products" in result.output


def test_install_reports_checksum_failure_and_exits_nonzero(tmp_path: Path) -> None:
    plan_file = _plan(tmp_path, sha256="0" * 64)
    result = CliRunner().invoke(
        _app(), ["product", "install", "--plan-file", str(plan_file)]
    )
    assert result.exit_code == 1
    payload = _json_stdout(result)
    assert payload["products"][0]["components"][0]["status"] == "failed"
    assert "sha256" in payload["products"][0]["components"][0]["detail"]


def test_product_group_has_four_commands() -> None:
    app = _app()
    group = next(g.typer_instance for g in app.registered_groups if g.name == "product")
    names = {command.name for command in group.registered_commands}
    assert names == {"install", "sync", "list", "remove"}


def test_list_is_empty_without_state() -> None:
    result = CliRunner().invoke(_app(), ["product", "list"])
    assert result.exit_code == 0
    assert _json_stdout(result) == {"products": [], "count": 0}


def test_product_group_is_registered_in_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.config import ClientConfig

    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    assert "product" in {group.name for group in app.registered_groups}


def test_list_is_json_by_default_even_when_cli_mode_is_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON — не «--json», а дефолт: команду читает установщик, не человек."""
    monkeypatch.setattr(output_module, "_mode", "text")
    result = CliRunner().invoke(_app(), ["product", "list"])
    assert result.exit_code == 0, result.output
    assert _json_stdout(result) == {"products": [], "count": 0}


def test_text_flag_renders_human_table(tmp_path: Path) -> None:
    plan_file = _plan(tmp_path)
    runner = CliRunner()
    assert runner.invoke(
        _app(), ["product", "install", "--plan-file", str(plan_file)]
    ).exit_code == 0

    listed = runner.invoke(_app(), ["product", "list", "--text"])

    assert listed.exit_code == 0, listed.output
    assert "Установленные продукты" in listed.stdout
    assert "demo" in listed.stdout
    assert not listed.stdout.lstrip().startswith("{")


def test_text_flag_keeps_json_channel_clean_on_error() -> None:
    """Ошибка при ``--text`` всё равно уходит в stderr, а не в данные."""
    result = CliRunner().invoke(_app(), ["product", "remove", "ghost", "--text"])
    assert result.exit_code == 1
    assert "не установлен" in result.output.lower()
