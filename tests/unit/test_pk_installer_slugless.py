"""PK-миграция (§3.E): slug-less skill → числовой id как имя папки/identity."""
from __future__ import annotations

import pytest

from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import (
    SkillInstaller,
    read_meta,
    skill_dir_name,
)


def test_skill_dir_name_prefers_slug() -> None:
    assert skill_dir_name("bitrix24", 42) == "bitrix24"


def test_skill_dir_name_falls_back_to_id() -> None:
    assert skill_dir_name(None, 42) == "42"
    assert skill_dir_name("", 42) == "42"  # пустой slug = «не задан»
    assert skill_dir_name(None, "42") == "42"


def test_skill_dir_name_requires_some_identity() -> None:
    with pytest.raises(ValueError):
        skill_dir_name(None, None)
    with pytest.raises(ValueError):
        skill_dir_name("", None)


def test_install_slugless_uses_id_as_folder(tmp_path) -> None:
    """slug=None + skill_id=77 → папка ~/.claude/skills/77/ + meta.skill_id."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    installer = SkillInstaller(target, store_dir=tmp_path / "store")
    result = installer.install(
        slug=None,
        skill_id=77,
        version="1.0.0",
        commit_sha="deadbeef00",
        repo_url=None,
        manifest={"version": "1.0.0", "description": "x", "files": []},
    )
    assert result.target_dir.name == "77"
    assert result.skill_id == "77"
    assert result.slug is None
    # SKILL.md-стаб использует id как name.
    assert "name: 77" in (result.target_dir / "SKILL.md").read_text(encoding="utf-8")
    meta = read_meta(result.target_dir)
    assert meta is not None
    assert meta["slug"] is None
    assert meta["skill_id"] == "77"


def test_reinstall_slugless_by_id_is_update(tmp_path) -> None:
    """Повторный install по тому же id попадает в ту же папку → update."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    installer = SkillInstaller(target, store_dir=tmp_path / "store")
    installer.install(
        slug=None, skill_id=77, version="1.0.0", commit_sha="aaa0000000",
        repo_url=None, manifest={"version": "1.0.0", "description": "x", "files": []},
    )
    r2 = installer.install(
        slug=None, skill_id=77, version="1.1.0", commit_sha="bbb0000000",
        repo_url=None, manifest={"version": "1.1.0", "description": "x", "files": []},
    )
    assert r2.is_update
    assert r2.target_dir.name == "77"
    assert read_meta(r2.target_dir)["version"] == "1.1.0"


def test_remove_slugless_by_id(tmp_path) -> None:
    """remove(skill_id=77) удаляет папку с числовым именем."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    installer = SkillInstaller(target, store_dir=tmp_path / "store")
    installer.install(
        slug=None, skill_id=77, version="1.0.0", commit_sha="ccc0000000",
        repo_url=None, manifest={"version": "1.0.0", "description": "x", "files": []},
    )
    res = installer.remove(slug=None, skill_id=77)
    assert res.removed is True
    assert res.target_dir.name == "77"
    assert not res.target_dir.exists()


def test_install_with_slug_still_uses_slug_folder(tmp_path) -> None:
    """Регрессия: при наличии slug папка по-прежнему slug, не id."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    installer = SkillInstaller(target, store_dir=tmp_path / "store")
    result = installer.install(
        slug="bitrix24",
        skill_id=42,
        version="1.0.0",
        commit_sha="ddd0000000",
        repo_url=None,
        manifest={"version": "1.0.0", "description": "x", "files": []},
    )
    assert result.target_dir.name == "bitrix24"
    assert result.slug == "bitrix24"
    meta = read_meta(result.target_dir)
    assert meta["slug"] == "bitrix24"
    assert meta["skill_id"] == "42"
