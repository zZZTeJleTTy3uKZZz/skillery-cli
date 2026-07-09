"""P0 BLOCKER (фикс 1): stub НЕ имеет права затирать живую установку.

Живой инцидент e2e: у скилла в хабе repo_url=null и версия «новее» →
auto-update / update вызывали installer.install → stub-ветка _materialize_store
→ _link_into_scope сносил существующую copy-установку (_force_rmtree) и ставил
junction на 112-байтовый stub. Реальный ~/.claude/skills/bitrix24 (32KB
SKILL.md, src/, tests/) был уничтожен.

Гарантии:
- installer.install со stub-источником (repo_url=None И local_src=None) поверх
  существующей НЕпустой установки (стор ИЛИ copy-scope) → skipped=True,
  skip_reason='stub-would-clobber', контент не тронут, ссылки не созданы;
- stub в пустое место — работает как раньше;
- stub поверх нашего же stub'а — разрешён (терять нечего, прежнее поведение);
- _maybe_auto_update пропускает bundle без repo_url (нечего обновлять);
- прогресс auto-update идёт в stderr, stdout остаётся чистым для --json.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core import linker
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller, read_meta, write_meta

_MANIFEST = {"version": "2.0.0", "description": "x", "files": []}


def _installer(tmp_path: Path) -> tuple[SkillInstaller, ClaudeCodeTarget, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    return SkillInstaller(target, store_dir=store), target, store


# ------------------------------------------------------------------
#  installer-уровень: guard stub-would-clobber
# ------------------------------------------------------------------
def test_stub_does_not_clobber_copy_scope_install(tmp_path: Path) -> None:
    """Живой сценарий: copy-установка (НЕ ссылка) с реальным контентом + meta.
    Stub-install обязан отказаться (skipped), ничего не тронув."""
    inst, target, store = _installer(tmp_path)
    slug_dir = target.slug_dir("bitrix24")
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text(
        "---\nname: bitrix24\nversion: 1.0.0\n---\n\n# Реальный навык\n" + "x" * 1000,
        encoding="utf-8",
    )
    (slug_dir / "src").mkdir()
    (slug_dir / "src" / "main.py").write_text("print('real')\n", encoding="utf-8")
    write_meta(slug_dir, {"slug": "bitrix24", "version": "1.0.0", "manifest": {}})

    res = inst.install(
        slug="bitrix24", version="2.0.0", commit_sha="",
        repo_url=None, manifest=_MANIFEST,
    )

    assert res.skipped is True
    assert res.skip_reason == "stub-would-clobber"
    # Контент цел: папка осталась копией (не ссылкой), файлы не тронуты.
    assert not linker.is_link(slug_dir)
    assert (slug_dir / "src" / "main.py").read_text(encoding="utf-8") == "print('real')\n"
    assert "Реальный навык" in (slug_dir / "SKILL.md").read_text(encoding="utf-8")
    # Meta не «обновлена» до версии, контента которой у нас нет.
    assert read_meta(slug_dir)["version"] == "1.0.0"
    # Стор не замусорен stub'ом.
    assert not (store / "bitrix24" / "SKILL.md").exists()


def test_stub_does_not_clobber_store_content(tmp_path: Path) -> None:
    """Стор уже содержит реальный контент (local-path установка) — stub-апдейт
    не должен трогать ни контент, ни meta-версию."""
    inst, target, store = _installer(tmp_path)
    src = tmp_path / "src-skill"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: demo\nversion: 1.0.0\n---\n\n# Real\n", encoding="utf-8"
    )
    (src / "lib.py").write_text("LIB = 1\n", encoding="utf-8")
    inst.install(
        slug="demo", version="1.0.0", commit_sha="",
        repo_url=None, local_src=src, manifest=_MANIFEST,
    )

    res = inst.install(
        slug="demo", version="9.9.9", commit_sha="",
        repo_url=None, manifest=_MANIFEST,
    )

    assert res.skipped is True
    assert res.skip_reason == "stub-would-clobber"
    assert (store / "demo" / "lib.py").read_text(encoding="utf-8") == "LIB = 1\n"
    assert read_meta(store / "demo")["version"] == "1.0.0"


def test_stub_into_fresh_place_still_installs(tmp_path: Path) -> None:
    inst, target, store = _installer(tmp_path)
    res = inst.install(
        slug="fresh", version="0.1.0", commit_sha="",
        repo_url=None, manifest=_MANIFEST,
    )
    assert res.skipped is False
    assert (store / "fresh" / "SKILL.md").exists()
    assert linker.is_link(target.slug_dir("fresh"))


def test_stub_over_our_stub_is_still_update(tmp_path: Path) -> None:
    """Существующая установка — наша же заглушка → переустановка разрешена
    (терять нечего; прежние тесты reinstall→update сохраняют силу)."""
    inst, target, store = _installer(tmp_path)
    inst.install(slug="wb", version="0.1.0", commit_sha="",
                 repo_url=None, manifest=_MANIFEST)
    r2 = inst.install(slug="wb", version="0.2.0", commit_sha="",
                      repo_url=None, manifest=_MANIFEST)
    assert r2.skipped is False
    assert r2.is_update is True
    assert read_meta(store / "wb")["version"] == "0.2.0"


def test_stub_guard_not_bypassed_by_force(tmp_path: Path) -> None:
    """Guard жёсткий: --force не позволяет stub'у затереть живой контент."""
    inst, target, _store = _installer(tmp_path)
    slug_dir = target.slug_dir("real")
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("# настоящий контент\n", encoding="utf-8")
    write_meta(slug_dir, {"slug": "real", "version": "1.0.0", "manifest": {}})

    res = inst.install(
        slug="real", version="2.0.0", commit_sha="",
        repo_url=None, manifest=_MANIFEST, force=True,
    )
    assert res.skipped is True
    assert "настоящий контент" in (slug_dir / "SKILL.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------
#  _maybe_auto_update: bundle без repo_url + чистота stdout
# ------------------------------------------------------------------
def _setup_auto_update_env(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed_version: str,
    bundle_version: str,
    bundle_repo_url: str | None,
    install_calls: list,
):
    """Окружение _maybe_auto_update: один установленный навык + стаб bundle."""
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    base = target.base_dir()
    skill_dir = base / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("hi", encoding="utf-8")
    (skill_dir / "_skill_meta.json").write_text(
        f'{{"slug": "demo", "version": "{installed_version}"}}',
        encoding="utf-8",
    )

    cfg = ClientConfig(
        store_dir=str(tmp_path / "store"),
        base_url="http://localhost:8000",
    )
    cfg.auto_update = True
    cfg.auto_update_cooldown_min = 60
    cfg.last_auto_update_at = None
    cfg.user_email = "x@y.io"
    monkeypatch.setattr(ClientConfig, "is_logged_in", lambda self: True)
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(
        main_mod, "load_tokens", lambda email: ("access-tok", "refresh-tok")
    )
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            return {
                "version": bundle_version,
                "commit_sha": "deadbeef",
                "repo_url": bundle_repo_url,
                "manifest": {"version": bundle_version, "files": []},
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)

    def _fake_install(self, **kw):
        install_calls.append(kw)

    monkeypatch.setattr(main_mod.SkillInstaller, "install", _fake_install)
    return main_mod, cfg


def test_auto_update_skips_bundle_without_repo_url(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle без repo_url = stub-источник — обновлять нечем, install НЕ зовём
    (живой инцидент: такой «апдейт» затирал реальный контент stub'ом)."""
    install_calls: list = []
    main_mod, cfg = _setup_auto_update_env(
        tmp_path, monkeypatch,
        installed_version="1.0.0", bundle_version="9.9.9",
        bundle_repo_url=None, install_calls=install_calls,
    )
    main_mod._maybe_auto_update(cfg)
    assert install_calls == []


def test_auto_update_installs_when_newer_with_repo(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_calls: list = []
    main_mod, cfg = _setup_auto_update_env(
        tmp_path, monkeypatch,
        installed_version="1.0.0", bundle_version="1.1.0",
        bundle_repo_url="https://git.example/demo.git", install_calls=install_calls,
    )
    main_mod._maybe_auto_update(cfg)
    assert len(install_calls) == 1
    assert install_calls[0]["version"] == "1.1.0"


def test_auto_update_progress_goes_to_stderr_not_stdout(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """В json-режиме stdout — машинный канал; «↑ auto-update ...» обязан идти
    в stderr (живой факт: прогресс ломал парсинг --json вывода)."""
    from skillery_cli import output as out_mod

    install_calls: list = []
    main_mod, cfg = _setup_auto_update_env(
        tmp_path, monkeypatch,
        installed_version="1.0.0", bundle_version="1.1.0",
        bundle_repo_url="https://git.example/demo.git", install_calls=install_calls,
    )
    monkeypatch.setattr(out_mod, "_mode", "json")
    main_mod._maybe_auto_update(cfg)
    captured = capsys.readouterr()
    assert install_calls  # апдейт состоялся
    assert captured.out == ""  # stdout чист
    assert "auto-update" in captured.err
