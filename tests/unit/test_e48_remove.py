"""E48 §8 — remove/uninstall: SkillInstaller.remove + cmd_remove CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller


def _install_stub(target: ClaudeCodeTarget, slug: str = "demo") -> Path:
    inst = SkillInstaller(target)
    res = inst.install(
        slug=slug,
        version="1.0.0",
        commit_sha="aaaa111111",
        repo_url=None,
        manifest={"version": "1.0.0", "description": "x", "files": []},
    )
    return res.target_dir


def test_remove_deletes_slug_dir(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    slug_dir = _install_stub(target)
    assert slug_dir.exists()

    inst = SkillInstaller(target)
    result = inst.remove(slug="demo")

    assert result.removed is True
    assert not slug_dir.exists()
    assert result.kept_local is False


def test_remove_keep_local_preserves_local_dir(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    slug_dir = _install_stub(target)
    (slug_dir / "_local").mkdir()
    (slug_dir / "_local" / "state.db").write_text("USER", encoding="utf-8")
    (slug_dir / "browser_profiles").mkdir()
    (slug_dir / "browser_profiles" / "p.json").write_text("{}", encoding="utf-8")
    (slug_dir / "SKILL.md").write_text("x", encoding="utf-8")

    inst = SkillInstaller(target)
    result = inst.remove(slug="demo", keep_local=True)

    assert result.removed is True
    assert result.kept_local is True
    # _local/ и browser_profiles/ остались; всё прочее удалено.
    assert (slug_dir / "_local" / "state.db").read_text(encoding="utf-8") == "USER"
    assert (slug_dir / "browser_profiles" / "p.json").exists()
    assert not (slug_dir / "SKILL.md").exists()
    assert not (slug_dir / "_skill_meta.json").exists()


def test_remove_nonexistent_returns_not_removed(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    inst = SkillInstaller(target)
    result = inst.remove(slug="ghost")
    assert result.removed is False


def test_remove_keep_local_when_no_local_removes_all(tmp_path: Path) -> None:
    """--keep-local но _local/ нет → удаляем всё, kept_local=False."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    slug_dir = _install_stub(target)
    inst = SkillInstaller(target)
    result = inst.remove(slug="demo", keep_local=True)
    assert result.removed is True
    assert result.kept_local is False
    assert not slug_dir.exists()


# ---------- CLI-level ----------
def test_cmd_remove_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """cmd_remove --json: эмитит структуру + трекает skill.uninstall."""
    import skills_hub_cli.__main__ as main_mod
    from skills_hub_cli.config import ClientConfig

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    slug_dir = _install_stub(target, slug="wb-api")
    assert slug_dir.exists()

    # Конфиг указывает на наш fake-target root.
    cfg = ClientConfig()
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)

    events: list[tuple] = []
    monkeypatch.setattr(
        main_mod, "track_skill_event",
        lambda et, **kw: events.append((et, kw)), raising=False
    )
    # force json mode
    from skills_hub_cli import output as out_mod
    monkeypatch.setattr(out_mod, "_mode", "json")

    main_mod.cmd_remove(slug="wb-api", scope="global", keep_local=False, project=None)

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["slug"] == "wb-api"
    assert payload["removed"] is True
    assert not slug_dir.exists()
    # event emitted
    assert any(et == "skill.uninstall" for et, _ in events)
