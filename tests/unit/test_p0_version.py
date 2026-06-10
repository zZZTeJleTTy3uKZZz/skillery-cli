"""Тест глобального флага ``--version`` (печатает __version__ и выходит)."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from skills_hub_cli import __version__
from skills_hub_cli.config import ClientConfig


def test_version_flag_prints_version_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Версия должна печататься даже БЕЗ логина (always-on).
    empty_cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: empty_cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_flag_works_when_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
