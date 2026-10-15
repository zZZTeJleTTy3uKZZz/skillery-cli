"""Persisted projection policy and per-command override validation."""
from pathlib import Path

import pytest

from skillery_cli.config import ClientConfig


def test_copy_projection_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    ClientConfig(skill_projection="copy").save(path)
    assert ClientConfig.load(path).skill_projection == "copy"


def test_default_projection_is_backward_compatible(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    ClientConfig().save(path)
    assert "skill_projection" not in path.read_text(encoding="utf-8")
    assert ClientConfig.load(path).skill_projection == "auto"


def test_invalid_projection_rejected() -> None:
    with pytest.raises(ValueError, match="skill_projection"):
        ClientConfig(skill_projection="unsafe")
