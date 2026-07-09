"""E48 §8.2 — incremental update.

`skillery update <slug>`:
1. читает старый manifest из _skill_meta.json,
2. git clone новой версии во временную папку,
3. diff по sha256,
4. копирует added + changed,
5. удаляет orphan (нет в новом manifest) КРОМЕ preserved_paths (_local/, ...),
6. применяет skill-filter,
7. перезаписывает _skill_meta.json.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from skillery_cli.core import installer as installer_mod
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller, read_meta


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest(files: dict[str, str], preserved: list[str] | None = None) -> dict[str, Any]:
    return {
        "version": "0.0.0",
        "description": "x",
        "files": [
            {"path": rel, "sha256": _sha(content), "size": len(content)}
            for rel, content in files.items()
        ],
        "preserved_paths": preserved if preserved is not None else ["_local/", "browser_profiles/"],
    }


def _make_fake_clone(file_layout: dict[str, str]):
    """subprocess.run(["git","clone",...,target]) → кладёт file_layout в target."""

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any):  # noqa: ARG001
        assert cmd[0] == "git" and cmd[1] == "clone", f"unexpected cmd: {cmd}"
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        for rel, content in file_layout.items():
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return fake_run


def _install_fresh(
    tmp_path: Path, monkeypatch, layout: dict[str, str]
) -> tuple[SkillInstaller, Path]:
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(layout))
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    inst = SkillInstaller(target, store_dir=tmp_path / "store")
    res = inst.install(
        slug="demo",
        version="1.0.0",
        commit_sha="aaaa111111",
        repo_url="https://example.com/demo.git",
        manifest=_manifest({k: v for k, v in layout.items() if k != ".git"}),
    )
    assert not res.is_update
    return inst, res.target_dir


def test_update_adds_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = {
        "SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\n# demo",
        "src/main.py": "print(1)",
    }
    inst, slug_dir = _install_fresh(tmp_path, monkeypatch, v1)

    v2 = {
        "SKILL.md": "---\nname: demo\nversion: 2.0.0\n---\n# demo",
        "src/main.py": "print(1)",
        "src/extra.py": "new file",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(v2))
    res = inst.install(
        slug="demo",
        version="2.0.0",
        commit_sha="bbbb222222",
        repo_url="https://example.com/demo.git",
        manifest=_manifest(v2),
    )

    assert res.is_update
    assert (slug_dir / "src" / "extra.py").read_text(encoding="utf-8") == "new file"
    assert read_meta(slug_dir)["version"] == "2.0.0"


def test_update_overwrites_changed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = {
        "SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\n# demo",
        "src/main.py": "old",
    }
    inst, slug_dir = _install_fresh(tmp_path, monkeypatch, v1)
    assert (slug_dir / "src" / "main.py").read_text(encoding="utf-8") == "old"

    v2 = {
        "SKILL.md": "---\nname: demo\nversion: 2.0.0\n---\n# demo",
        "src/main.py": "NEW CONTENT",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(v2))
    inst.install(
        slug="demo",
        version="2.0.0",
        commit_sha="bbbb222222",
        repo_url="https://example.com/demo.git",
        manifest=_manifest(v2),
    )
    assert (slug_dir / "src" / "main.py").read_text(encoding="utf-8") == "NEW CONTENT"


def test_update_removes_orphan_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = {
        "SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\n# demo",
        "src/main.py": "code",
        "src/legacy.py": "to be removed",
    }
    inst, slug_dir = _install_fresh(tmp_path, monkeypatch, v1)
    assert (slug_dir / "src" / "legacy.py").exists()

    v2 = {
        "SKILL.md": "---\nname: demo\nversion: 2.0.0\n---\n# demo",
        "src/main.py": "code",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(v2))
    inst.install(
        slug="demo",
        version="2.0.0",
        commit_sha="bbbb222222",
        repo_url="https://example.com/demo.git",
        manifest=_manifest(v2),
    )
    assert not (slug_dir / "src" / "legacy.py").exists()
    assert (slug_dir / "src" / "main.py").exists()


def test_update_preserves_local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_local/ — пользовательский state — НЕ удаляется при update (нет в new manifest)."""
    v1 = {
        "SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\n# demo",
        "src/main.py": "code",
    }
    inst, slug_dir = _install_fresh(tmp_path, monkeypatch, v1)
    # Пользователь создал локальное состояние после install.
    (slug_dir / "_local").mkdir()
    (slug_dir / "_local" / "session.db").write_text("USER DATA", encoding="utf-8")
    (slug_dir / "browser_profiles").mkdir()
    (slug_dir / "browser_profiles" / "default.json").write_text("{}", encoding="utf-8")

    v2 = {
        "SKILL.md": "---\nname: demo\nversion: 2.0.0\n---\n# demo",
        "src/main.py": "code2",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(v2))
    inst.install(
        slug="demo",
        version="2.0.0",
        commit_sha="bbbb222222",
        repo_url="https://example.com/demo.git",
        manifest=_manifest(v2),
    )
    # Локальный state сохранён.
    assert (slug_dir / "_local" / "session.db").read_text(encoding="utf-8") == "USER DATA"
    assert (slug_dir / "browser_profiles" / "default.json").exists()
    # Обновлённый файл — новый.
    assert (slug_dir / "src" / "main.py").read_text(encoding="utf-8") == "code2"


def test_update_preserves_local_even_with_files_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия: новая версия с `files:` allowlist НЕ должна стирать _local/.

    apply_skill_filter в allowlist-режиме удаляет всё не в списке; но
    пользовательский _local/ обязан сохраниться при update (ТЗ §8.2).
    """
    v1 = {
        "SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\n# demo",
        "src/main.py": "code",
    }
    inst, slug_dir = _install_fresh(tmp_path, monkeypatch, v1)
    (slug_dir / "_local").mkdir()
    (slug_dir / "_local" / "state.db").write_text("USER DATA", encoding="utf-8")

    # Новая версия объявляет жёсткий allowlist, который НЕ упоминает _local.
    skill_md_v2 = (
        "---\nname: demo\nversion: 2.0.0\nfiles:\n  - SKILL.md\n  - src/**\n---\n# demo"
    )
    v2 = {
        "SKILL.md": skill_md_v2,
        "src/main.py": "code2",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(v2))
    inst.install(
        slug="demo",
        version="2.0.0",
        commit_sha="bbbb222222",
        repo_url="https://example.com/demo.git",
        manifest=_manifest(v2),
    )
    # _local/ выжил несмотря на allowlist-фильтр.
    assert (slug_dir / "_local" / "state.db").read_text(encoding="utf-8") == "USER DATA"
    assert (slug_dir / "src" / "main.py").read_text(encoding="utf-8") == "code2"


def test_update_reports_diff_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = {
        "SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\n# demo",
        "a.py": "a",
        "b.py": "b",
    }
    inst, slug_dir = _install_fresh(tmp_path, monkeypatch, v1)

    v2 = {
        "SKILL.md": "---\nname: demo\nversion: 2.0.0\n---\n# demo",  # changed (version)
        "a.py": "a",  # unchanged
        "c.py": "c",  # added; b.py removed
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(v2))
    res = inst.install(
        slug="demo",
        version="2.0.0",
        commit_sha="bbbb222222",
        repo_url="https://example.com/demo.git",
        manifest=_manifest(v2),
    )
    assert res.update_diff is not None
    assert res.update_diff["added"] == 1  # c.py
    assert res.update_diff["changed"] == 1  # SKILL.md
    assert res.update_diff["removed"] == 1  # b.py


def test_update_rejects_traversal_in_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если новая версия repo содержит symlink наружу — update падает (guard)."""
    v1 = {
        "SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\n# demo",
        "main.py": "code",
    }
    inst, slug_dir = _install_fresh(tmp_path, monkeypatch, v1)

    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")

    def fake_run_with_symlink(cmd: list[str], *a: Any, **k: Any):
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            "---\nname: demo\nversion: 2.0.0\n---\n# demo", encoding="utf-8"
        )
        # Вредоносный symlink на хост-секрет, под видом обычного файла skill'а.
        try:
            (target / "evil.txt").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(installer_mod.subprocess, "run", fake_run_with_symlink)
    from skillery_cli.core.installer import PathTraversalError

    # Manifest объявляет evil.txt как обычный файл → diff попытается его скопировать
    # → _safe_copy_file обязан отвергнуть symlink-escape.
    new_manifest = _manifest(
        {"SKILL.md": "---\nname: demo\nversion: 2.0.0\n---\n# demo", "evil.txt": "SECRET"}
    )
    with pytest.raises((PathTraversalError, RuntimeError)):
        inst.install(
            slug="demo",
            version="2.0.0",
            commit_sha="bbbb222222",
            repo_url="https://example.com/demo.git",
            manifest=new_manifest,
        )
    # Секрет не утёк в slug_dir.
    assert not (slug_dir / "evil.txt").exists()
