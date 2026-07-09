"""Тесты IAgentTarget."""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core.agents import (
    ClaudeCodeTarget,
    CodexTarget,
    get_target,
)


def test_claude_code_layout(tmp_path: Path) -> None:
    t = ClaudeCodeTarget(root=tmp_path / ".claude")
    assert t.base_dir() == tmp_path / ".claude" / "skills"
    assert t.slug_dir("foo") == tmp_path / ".claude" / "skills" / "foo"
    assert "_local/" in t.preserved_paths()


def test_codex_layout(tmp_path: Path) -> None:
    t = CodexTarget(root=tmp_path / ".codex")
    assert t.slug_dir("bar") == tmp_path / ".codex" / "skills" / "bar"


def test_get_target_by_name() -> None:
    assert get_target("claude_code").name == "claude_code"
    assert get_target("codex").name == "codex"
    with pytest.raises(ValueError):
        get_target("unknown_agent")


def test_exists_detection(tmp_path: Path) -> None:
    t = ClaudeCodeTarget(root=tmp_path / "noop")
    assert not t.exists()
    (tmp_path / "yes").mkdir()
    t2 = ClaudeCodeTarget(root=tmp_path / "yes")
    assert t2.exists()
