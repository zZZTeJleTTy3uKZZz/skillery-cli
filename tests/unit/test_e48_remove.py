"""remove/uninstall под модель стор+линк: снятие ссылки, copy-fallback keep_local, purge."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills_hub_cli.core import linker
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _inst(tmp_path: Path) -> tuple[SkillInstaller, ClaudeCodeTarget, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    return SkillInstaller(target, store_dir=store), target, store


def test_remove_unlinks_scope_keeps_store(tmp_path: Path) -> None:
    inst, target, store = _inst(tmp_path)
    res = inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                       repo_url=None, manifest=_MANIFEST)
    link = res.target_dir
    assert linker.is_link(link)

    rm = inst.remove(slug="demo")
    assert rm.removed is True
    assert not link.exists()            # ссылка снята
    assert (store / "demo" / "SKILL.md").exists()  # стор цел


def test_remove_purge_deletes_store(tmp_path: Path) -> None:
    inst, target, store = _inst(tmp_path)
    inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                 repo_url=None, manifest=_MANIFEST)
    rm = inst.remove(slug="demo", purge=True)
    assert rm.removed is True
    assert rm.purged is True
    assert not (store / "demo").exists()


def test_remove_copy_fallback_keep_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если scope — copy-fallback (не ссылка), keep_local сохраняет _local/."""
    inst, target, store = _inst(tmp_path)
    monkeypatch.setattr(
        linker, "create_link",
        lambda link, tgt: (_ for _ in ()).throw(OSError("no perms")),
    )
    res = inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                       repo_url=None, manifest=_MANIFEST)
    assert res.linked is False
    slug_dir = res.target_dir
    (slug_dir / "_local").mkdir()
    (slug_dir / "_local" / "state.db").write_text("USER", encoding="utf-8")

    rm = inst.remove(slug="demo", keep_local=True)
    assert rm.removed is True
    assert rm.kept_local is True
    assert (slug_dir / "_local" / "state.db").read_text(encoding="utf-8") == "USER"
    assert not (slug_dir / "SKILL.md").exists()


def test_remove_nonexistent_returns_not_removed(tmp_path: Path) -> None:
    inst, target, store = _inst(tmp_path)
    rm = inst.remove(slug="ghost")
    assert rm.removed is False


def test_cmd_remove_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """cmd_remove --json: эмитит структуру + трекает skill.uninstall."""
    import skills_hub_cli.__main__ as main_mod
    from skills_hub_cli.config import ClientConfig

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)
    res = inst.install(slug="wb-api", version="1.0.0", commit_sha="a1",
                       repo_url=None, manifest=_MANIFEST)
    assert res.target_dir.exists()

    cfg = ClientConfig(store_dir=str(store))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    events: list[tuple] = []
    monkeypatch.setattr(main_mod, "track_skill_event",
                        lambda et, **kw: events.append((et, kw)), raising=False)
    from skills_hub_cli import output as out_mod
    monkeypatch.setattr(out_mod, "_mode", "json")

    main_mod.cmd_remove(slug="wb-api", scope="global", keep_local=False,
                        project=None, purge=False)

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["slug"] == "wb-api"
    assert payload["removed"] is True
    assert not res.target_dir.exists()
    assert any(et == "skill.uninstall" for et, _ in events)
