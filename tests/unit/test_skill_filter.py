"""Тесты для skill_filter: .skillignore + manifest.files allowlist."""
from __future__ import annotations

from pathlib import Path

from skillery_cli.core.skill_filter import (
    apply_skill_filter,
    parse_skill_md_files_allowlist,
)


def test_no_skillignore_no_allowlist_keeps_all(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("# minimal", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("test", encoding="utf-8")
    result = apply_skill_filter(tmp_path)
    assert result["removed"] == 0
    assert (tmp_path / "tests" / "test_x.py").exists()


def test_skillignore_removes_matched(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("# minimal", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("test", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("code", encoding="utf-8")
    (tmp_path / ".skillignore").write_text("tests/\n", encoding="utf-8")

    result = apply_skill_filter(tmp_path)

    assert result["removed"] >= 1
    assert not (tmp_path / "tests").exists()
    assert (tmp_path / "src" / "main.py").exists()
    assert (tmp_path / "SKILL.md").exists()


def test_skillignore_with_glob_patterns(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("# minimal", encoding="utf-8")
    (tmp_path / "BACKLOG.md").write_text("dev", encoding="utf-8")
    (tmp_path / "_NOTES_FOR_DOC_AGENT.md").write_text("dev", encoding="utf-8")
    (tmp_path / "README.md").write_text("user", encoding="utf-8")
    (tmp_path / ".skillignore").write_text(
        "BACKLOG.md\n_NOTES_FOR_*.md\n", encoding="utf-8"
    )

    apply_skill_filter(tmp_path)

    assert not (tmp_path / "BACKLOG.md").exists()
    assert not (tmp_path / "_NOTES_FOR_DOC_AGENT.md").exists()
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / "SKILL.md").exists()


def test_allowlist_in_skill_md_overrides_ignore(tmp_path: Path) -> None:
    skill_md_content = """---
name: test
files:
  - SKILL.md
  - src/**
---
# Test"""
    (tmp_path / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "code.py").write_text("code", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.py").write_text("test", encoding="utf-8")
    (tmp_path / "README.md").write_text("user", encoding="utf-8")

    apply_skill_filter(tmp_path)

    assert (tmp_path / "SKILL.md").exists()
    assert (tmp_path / "src" / "code.py").exists()
    assert not (tmp_path / "tests").exists()  # not in allowlist
    assert not (tmp_path / "README.md").exists()  # not in allowlist


def test_parse_skill_md_no_frontmatter_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text("# No frontmatter", encoding="utf-8")
    assert parse_skill_md_files_allowlist(p) is None


def test_parse_skill_md_no_files_field_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\n---\n# body", encoding="utf-8")
    assert parse_skill_md_files_allowlist(p) is None


def test_parse_skill_md_files_field(tmp_path: Path) -> None:
    p = tmp_path / "SKILL.md"
    p.write_text(
        """---
name: x
files:
  - SKILL.md
  - "src/**"
  - 'tests/*.py'
---
# body""",
        encoding="utf-8",
    )
    result = parse_skill_md_files_allowlist(p)
    assert result == ["SKILL.md", "src/**", "tests/*.py"]


def test_preserves_git_directory(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("# m", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (tmp_path / ".skillignore").write_text("*\n", encoding="utf-8")

    apply_skill_filter(tmp_path)

    assert (tmp_path / ".git" / "HEAD").exists()
    assert (tmp_path / "SKILL.md").exists()  # SKILL.md preserved


def test_parse_skill_md_missing_file_returns_none(tmp_path: Path) -> None:
    """Если SKILL.md не существует — None (не падает)."""
    p = tmp_path / "SKILL.md"  # not created
    assert parse_skill_md_files_allowlist(p) is None


def test_skillignore_comments_and_blank_lines_ignored(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("# m", encoding="utf-8")
    (tmp_path / "keep.md").write_text("k", encoding="utf-8")
    (tmp_path / "drop.md").write_text("d", encoding="utf-8")
    (tmp_path / ".skillignore").write_text(
        "# comment line\n\ndrop.md\n   # indented comment\n",
        encoding="utf-8",
    )

    apply_skill_filter(tmp_path)

    assert (tmp_path / "keep.md").exists()
    assert not (tmp_path / "drop.md").exists()


def test_allowlist_preserves_skill_md_and_skillignore_implicitly(tmp_path: Path) -> None:
    """Если allowlist не содержит SKILL.md/.skillignore — они всё равно preserved."""
    skill_md_content = """---
files:
  - src/**
---
# Test"""
    (tmp_path / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    (tmp_path / ".skillignore").write_text("# noop\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "junk.md").write_text("junk", encoding="utf-8")

    apply_skill_filter(tmp_path)

    assert (tmp_path / "SKILL.md").exists()
    assert (tmp_path / ".skillignore").exists()
    assert (tmp_path / "src" / "a.py").exists()
    assert not (tmp_path / "junk.md").exists()


def test_kept_count_excludes_git(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("# m", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (tmp_path / ".git" / "config").write_text("cfg", encoding="utf-8")
    (tmp_path / "extra.md").write_text("x", encoding="utf-8")

    result = apply_skill_filter(tmp_path)

    # SKILL.md + extra.md = 2; .git/HEAD + .git/config excluded
    assert result["kept"] == 2
