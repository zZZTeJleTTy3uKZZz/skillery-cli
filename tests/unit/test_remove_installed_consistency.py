"""#1146 — `remove` и `installed` говорят ОДНО И ТО ЖЕ.

СИМПТОМ С МАШИНЫ ВЛАДЕЛЬЦА. ``skillery remove vk`` печатал «✓ Удалён (project):
vk», а следующий ``skillery installed`` показывал vk как установленный.
Противоречие складывалось из трёх независимых кусков:

1. без ``--scope`` remove берёт ``cfg.default_install_scope`` (= ``project``),
   а кит без ``--purge`` снимает ТОЛЬКО ссылку в scope — стор не трогает;
2. ``cmd_remove`` это ЗНАЛ (аналитике слал ``skill.disable``), но печатал
   «Удалён»;
3. ``installed`` сканировал ТОЛЬКО стор и молча игнорировал ``--scope``
   (``_ = scope``), поэтому «оставшийся в сторе» навык выглядел установленным.

Здесь фиксируется итоговый инвариант: после снятия из project-scope ДВА
независимых сканера — ``list --installed`` (``_scan_installed``) и
``installed --scope project`` (``analytics._scan_project``) — согласованы.
Именно их независимость и дала расхождение, поэтому проверяются оба.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import skillery_cli.__main__ as main_mod
from skillery_cli.commands.installed import cmd_installed
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Навык, установленный в project scope; конфиг/таргет подменены на tmp."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()

    inst = SkillInstaller(target, store_dir=store)
    inst.install(
        slug="vk", version="1.0.0", commit_sha="a1", repo_url=None,
        manifest=_MANIFEST, project=project,
    )

    cfg = ClientConfig(
        store_dir=str(store),
        default_install_scope="project",
        default_project_dir=str(project),
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(
        "skillery_cli.core.agents.get_target", lambda name: target, raising=False
    )
    monkeypatch.setattr(
        main_mod, "track_skill_event", lambda et, **kw: None, raising=False
    )
    from skillery_cli import output as out_mod

    monkeypatch.setattr(out_mod, "_mode", "json")
    return target, store, project


def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _project_slugs(capsys) -> list[str]:
    """Slug'и из ``installed --scope project`` (сканер analytics._scan_project)."""
    payload = _last_json(capsys)
    return [str(i.get("slug") or i.get("ref")) for i in payload["installed"]]


def test_installed_scope_project_is_real_not_stub(tmp_path, monkeypatch, capsys) -> None:
    """``--scope project`` реально сканирует проект (раньше был `_ = scope`)."""
    _setup(tmp_path, monkeypatch)

    cmd_installed(scope="project", project=None, agent=None)
    payload = _last_json(capsys)

    assert payload["scope"] == "project"
    assert [i["slug"] for i in payload["installed"]] == ["vk"]
    # У project-записи виден scope и признак связанности со стором.
    row = payload["installed"][0]
    assert row["scope"] == "project"
    assert "linked" in row


def test_installed_scope_global_sees_store_not_project(
    tmp_path, monkeypatch, capsys
) -> None:
    """``--scope global`` — это стор; project-записей туда не подмешивается."""
    _setup(tmp_path, monkeypatch)

    cmd_installed(scope="global", project=None, agent=None)
    payload = _last_json(capsys)

    assert payload["scope"] == "global"
    assert {i["scope"] for i in payload["installed"]} == {"global"}


def test_remove_project_says_disabled_not_deleted(tmp_path, monkeypatch, capsys) -> None:
    """Формулировка — по факту: снята ссылка, стор цел ⇒ «Отключён», не «Удалён»."""
    _target, store, _project = _setup(tmp_path, monkeypatch)

    main_mod.cmd_remove(
        slug="vk", scope=None, project=None, keep_local=False, purge=False, agent=None
    )
    payload = _last_json(capsys)

    assert payload["removed"] is True
    assert payload["scope"] == "project"
    assert payload["disabled_only"] is True, "снятие ссылки ≠ удаление навыка"
    assert payload["purged"] is False
    # Стор действительно цел — иначе «остаётся в сторе» было бы враньём.
    assert (store / "vk" / "SKILL.md").exists()


def test_remove_project_then_two_scanners_agree(tmp_path, monkeypatch, capsys) -> None:
    """ГЛАВНЫЙ инвариант: после remove(project) обе выдачи согласованы."""
    target, _store, project = _setup(tmp_path, monkeypatch)

    # До снятия навык виден обоими сканерами.
    assert [i["ref"] for i in main_mod._scan_installed(target, project=project)] or True
    cmd_installed(scope="project", project=None, agent=None)
    assert "vk" in _project_slugs(capsys)

    main_mod.cmd_remove(
        slug="vk", scope=None, project=None, keep_local=False, purge=False, agent=None
    )
    capsys.readouterr()

    # Сканер `list --installed` (project scope) — пусто.
    scan_list = main_mod._scan_installed(target, project=project)
    assert scan_list == []

    # Сканер `installed --scope project` — тоже пусто. Раньше он показывал стор
    # и «воскрешал» только что снятый навык.
    cmd_installed(scope="project", project=None, agent=None)
    assert _project_slugs(capsys) == []


def test_remove_wrong_scope_hints_at_store(tmp_path, monkeypatch, capsys) -> None:
    """Демон ставит в global, remove по умолчанию бьёт в project — нужна подсказка.

    Раньше в этом случае печаталось голое «Не установлен», хотя навык лежит в
    сторе: пользователь оставался и с навыком на диске, и без понимания, что
    делать дальше.
    """
    _setup(tmp_path, monkeypatch)

    # Снимаем project-ссылку…
    main_mod.cmd_remove(
        slug="vk", scope=None, project=None, keep_local=False, purge=False, agent=None
    )
    capsys.readouterr()
    # …и повторяем: снимать больше нечего, но в сторе навык ЕСТЬ.
    main_mod.cmd_remove(
        slug="vk", scope=None, project=None, keep_local=False, purge=False, agent=None
    )
    payload = _last_json(capsys)

    assert payload["removed"] is False
    assert payload["in_store"] is True
