"""E48 §10 — path-traversal / symlink guard при копировании cloned-репо в slug_dir.

`safe_copy_tree(src, dst)` копирует содержимое src → dst, но отвергает
любой путь который вырывается за пределы dst:
  - `..` сегменты в относительном пути,
  - абсолютные пути,
  - симлинки, чья цель резолвится наружу dst.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skills_hub_cli.core.installer import PathTraversalError, safe_copy_tree


def test_safe_copy_tree_copies_normal_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "SKILL.md").write_text("hello", encoding="utf-8")
    (src / "sub" / "a.py").write_text("code", encoding="utf-8")

    safe_copy_tree(src, dst)

    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "hello"
    assert (dst / "sub" / "a.py").read_text(encoding="utf-8") == "code"


def test_safe_copy_tree_skips_dot_git(tmp_path: Path) -> None:
    """.git внутри source НЕ копируется в slug_dir (мы и так храним meta)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / ".git" / "objects").mkdir(parents=True)
    (src / ".git" / "config").write_text("[core]", encoding="utf-8")
    (src / "SKILL.md").write_text("x", encoding="utf-8")

    safe_copy_tree(src, dst)

    assert (dst / "SKILL.md").exists()
    assert not (dst / ".git").exists()


def test_safe_copy_tree_rejects_symlink_escaping_dst(tmp_path: Path) -> None:
    """Симлинк, указывающий наружу dst (на секрет), должен быть отвергнут."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("x", encoding="utf-8")
    link = src / "evil"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/privilege")

    dst = tmp_path / "dst"
    with pytest.raises(PathTraversalError):
        safe_copy_tree(src, dst)


def test_safe_copy_tree_rejects_symlinked_dir_escaping_dst(tmp_path: Path) -> None:
    """Симлинк-директория наружу тоже отвергается (не следуем по ней)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("leak", encoding="utf-8")

    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("x", encoding="utf-8")
    link = src / "outdir"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/privilege")

    dst = tmp_path / "dst"
    with pytest.raises(PathTraversalError):
        safe_copy_tree(src, dst)


def test_safe_copy_tree_allows_internal_symlink(tmp_path: Path) -> None:
    """Симлинк, указывающий ВНУТРЬ src (резолвится внутрь dst), допустим."""
    src = tmp_path / "src"
    src.mkdir()
    real = src / "real.txt"
    real.write_text("data", encoding="utf-8")
    link = src / "alias.txt"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/privilege")

    dst = tmp_path / "dst"
    # Не должно бросать; алиас резолвится внутрь src → внутрь dst.
    safe_copy_tree(src, dst)
    assert (dst / "real.txt").read_text(encoding="utf-8") == "data"


def test_assert_within_rejects_dotdot_escape(tmp_path: Path) -> None:
    """`_assert_within` отвергает путь с `..`, вырывающийся за base (все платформы)."""
    from skills_hub_cli.core.installer import _assert_within

    dst = tmp_path / "dst"
    dst.mkdir()
    with pytest.raises(PathTraversalError):
        _assert_within(dst, dst / ".." / "escape.txt")
    with pytest.raises(PathTraversalError):
        _assert_within(dst, dst / "sub" / ".." / ".." / "escape.txt")
    # Нормальные пути — ок.
    _assert_within(dst, dst / "ok.txt")
    _assert_within(dst, dst / "sub" / "deep" / "ok.txt")


def test_assert_within_rejects_absolute_outside(tmp_path: Path) -> None:
    """Абсолютный путь вне base отвергается."""
    from skills_hub_cli.core.installer import _assert_within

    dst = tmp_path / "dst"
    dst.mkdir()
    outside = tmp_path / "elsewhere" / "x.txt"
    with pytest.raises(PathTraversalError):
        _assert_within(dst, outside)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: разные диски")
def test_assert_within_rejects_different_drive(tmp_path: Path) -> None:
    """Windows: путь на другом диске → traversal (commonpath ValueError)."""
    from skills_hub_cli.core.installer import _assert_within

    dst = tmp_path / "dst"
    dst.mkdir()
    # Выбираем диск, отличный от диска tmp_path.
    other_drive = "Z:" if str(dst)[0].upper() != "Z" else "Y:"
    with pytest.raises(PathTraversalError):
        _assert_within(dst, Path(f"{other_drive}\\evil.txt"))
