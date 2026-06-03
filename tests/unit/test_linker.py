"""Тесты core/linker: junction (Windows) / symlink (POSIX) + безопасное удаление."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from skills_hub_cli.core import linker


def _make_store(tmp_path: Path) -> Path:
    store = tmp_path / "store" / "demo"
    store.mkdir(parents=True)
    (store / "SKILL.md").write_text("hi", encoding="utf-8")
    return store


def test_create_link_and_detect(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    link = tmp_path / ".claude" / "skills" / "demo"
    kind = linker.create_link(link, store)
    assert kind in ("junction", "symlink")
    assert linker.is_link(link)
    # Через ссылку виден контент стора.
    assert (link / "SKILL.md").read_text(encoding="utf-8") == "hi"
    # link_target указывает на стор.
    tgt = linker.link_target(link)
    assert tgt is not None
    assert os.path.normcase(str(tgt)) == os.path.normcase(str(store.resolve()))


def test_create_link_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    link = tmp_path / ".claude" / "skills" / "demo"
    linker.create_link(link, store)
    # Повторный вызов на тот же target — без ошибок, ссылка цела.
    linker.create_link(link, store)
    assert linker.is_link(link)
    assert (link / "SKILL.md").exists()


def test_create_link_repoints_to_new_target(tmp_path: Path) -> None:
    store_a = _make_store(tmp_path)
    store_b = tmp_path / "store" / "demo2"
    store_b.mkdir(parents=True)
    (store_b / "SKILL.md").write_text("bye", encoding="utf-8")
    link = tmp_path / ".claude" / "skills" / "demo"
    linker.create_link(link, store_a)
    linker.create_link(link, store_b)  # перенацелить
    assert (link / "SKILL.md").read_text(encoding="utf-8") == "bye"


def test_remove_link_does_not_touch_target(tmp_path: Path) -> None:
    """КРИТИЧНО: удаление ссылки не трогает содержимое стора."""
    store = _make_store(tmp_path)
    link = tmp_path / ".claude" / "skills" / "demo"
    linker.create_link(link, store)
    assert linker.remove_link(link) is True
    assert not link.exists()
    # Стор и его файлы целы.
    assert store.exists()
    assert (store / "SKILL.md").read_text(encoding="utf-8") == "hi"


def test_remove_link_on_plain_dir_is_noop(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x", encoding="utf-8")
    assert linker.remove_link(plain) is False
    assert (plain / "f.txt").exists()


def test_is_link_false_for_plain_dir(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    assert linker.is_link(d) is False
