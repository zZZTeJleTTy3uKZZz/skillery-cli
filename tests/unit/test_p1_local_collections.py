"""Тесты P1 C4 — локальные коллекции БЕЗ хаба (E10).

Покрытие:
- core/local_collections.py: CRUD поверх <config_dir>/collections.toml
  (уважает SKILLS_HUB_CONFIG_DIR), идемпотентность add/remove, валидация имён;
- commands/collection.py: cmd_collection_*_local — оффлайн CRUD без логина,
  warning при добавлении слага, которого нет в сторе;
- install-local: чисто-сторовый сценарий БЕЗ сети (store → link, hub не
  дёргается), отсутствующий слаг без логина → skipped (команда не падает),
  hub-докачка через общий _install_chain когда залогинен;
- build_app: локальные подкоманды ALWAYS-ON (без логина), серверные
  (collections list / collection show / collection install) остаются
  гейтнутыми skill.read / skill.install.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

# Импорт __main__ на уровне модуля (как в test_p0fix_lifecycle_no_login):
# его module-level init_output_mode() сбрасывает output-режим — если оставить
# импорт ленивым (через _ensure_install_helpers внутри команды), он перетёр
# бы monkeypatch json-режима ПОСРЕДИ первого install-local теста.
import skills_hub_cli.__main__ as main_mod
import skills_hub_cli.config as config_module
from skills_hub_cli import output as out_mod
from skills_hub_cli.commands import _common, collection as coll_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core import linker, local_collections, project_manifest
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


@pytest.fixture()
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный config-dir: collections.toml живёт в tmp, профиль сброшен."""
    d = tmp_path / "cfg"
    monkeypatch.setenv("SKILLS_HUB_CONFIG_DIR", str(d))
    monkeypatch.delenv("SKILLS_HUB_PROFILE", raising=False)
    monkeypatch.setattr(config_module, "_ACTIVE_PROFILE", None)
    return d


def _json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(out_mod, "_mode", "json")


def _last_json(out: str) -> dict | list:
    return json.loads(out.strip().splitlines()[-1])


def _offline_cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ClientConfig:
    """Незалогиненный конфиг со стором в tmp."""
    cfg = ClientConfig(store_dir=str(tmp_path / "store"))
    assert cfg.is_logged_in() is False
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    return cfg


def _logged_in_cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ClientConfig:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "skill.install"],
        store_dir=str(tmp_path / "store"),
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))
    return cfg


# ======================================================
#  core/local_collections — CRUD поверх collections.toml
# ======================================================
def test_create_and_roundtrip(cfg_dir: Path) -> None:
    coll = local_collections.create("web-pack", title="Web навыки")
    assert coll == {"title": "Web навыки", "skills": []}
    # Файл лёг в config-dir (уважает SKILLS_HUB_CONFIG_DIR).
    path = local_collections.collections_path()
    assert path == cfg_dir / "collections.toml"
    assert path.exists()
    loaded = local_collections.load_all()
    assert loaded["web-pack"]["title"] == "Web навыки"
    assert loaded["web-pack"]["skills"] == []


def test_create_default_title_is_name(cfg_dir: Path) -> None:
    coll = local_collections.create("pack")
    assert coll["title"] == "pack"


def test_create_duplicate_raises(cfg_dir: Path) -> None:
    local_collections.create("pack")
    with pytest.raises(local_collections.LocalCollectionError) as ei:
        local_collections.create("pack")
    assert ei.value.code == "ALREADY_EXISTS"


def test_create_invalid_name_raises(cfg_dir: Path) -> None:
    with pytest.raises(local_collections.LocalCollectionError) as ei:
        local_collections.create("bad name!")
    assert ei.value.code == "VALIDATION"


def test_add_remove_skill_idempotent(cfg_dir: Path) -> None:
    local_collections.create("pack")
    coll, added = local_collections.add_skill("pack", "demo")
    assert added is True
    assert coll["skills"] == ["demo"]
    # Повторное добавление — идемпотентно (без дубля).
    coll, added = local_collections.add_skill("pack", "demo")
    assert added is False
    assert coll["skills"] == ["demo"]
    coll, removed = local_collections.remove_skill("pack", "demo")
    assert removed is True
    assert coll["skills"] == []
    coll, removed = local_collections.remove_skill("pack", "demo")
    assert removed is False


def test_add_to_missing_collection_raises(cfg_dir: Path) -> None:
    with pytest.raises(local_collections.LocalCollectionError) as ei:
        local_collections.add_skill("ghost", "demo")
    assert ei.value.code == "NOT_FOUND"


def test_delete_collection(cfg_dir: Path) -> None:
    local_collections.create("pack")
    local_collections.delete("pack")
    assert local_collections.load_all() == {}
    with pytest.raises(local_collections.LocalCollectionError) as ei:
        local_collections.delete("pack")
    assert ei.value.code == "NOT_FOUND"


def test_missing_in_store(cfg_dir: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "have").mkdir(parents=True)
    missing = local_collections.missing_in_store(["have", "ghost"], store)
    assert missing == ["ghost"]


# ======================================================
#  Команды CRUD — оффлайн, без логина и сети
# ======================================================
def test_cmd_crud_local_offline(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)

    coll_mod.cmd_collection_create_local(name="pack", title="Мой набор")
    payload = _last_json(capsys.readouterr().out)
    assert payload["event"] == "local_collection_created"
    assert payload["name"] == "pack"
    assert payload["title"] == "Мой набор"

    # add-local: слага нет в сторе → warning в stderr, но добавлен всё равно.
    coll_mod.cmd_collection_add_local(name="pack", skill_slug="demo")
    captured = capsys.readouterr()
    payload = _last_json(captured.out)
    assert payload["added"] is True
    assert payload["in_store"] is False
    assert payload["skills"] == ["demo"]
    assert '"warn"' in captured.err

    # list-local: count + missing_in_store.
    coll_mod.cmd_collection_list_local()
    rows = _last_json(capsys.readouterr().out)
    assert rows == [
        {
            "name": "pack",
            "title": "Мой набор",
            "skills_count": 1,
            "skills": ["demo"],
            "missing_in_store": ["demo"],
        }
    ]

    # remove-local.
    coll_mod.cmd_collection_remove_local(name="pack", skill_slug="demo")
    payload = _last_json(capsys.readouterr().out)
    assert payload["removed"] is True
    assert payload["skills"] == []

    # delete-local.
    coll_mod.cmd_collection_delete_local(name="pack")
    payload = _last_json(capsys.readouterr().out)
    assert payload["event"] == "local_collection_deleted"
    assert local_collections.load_all() == {}


def test_cmd_add_local_no_warning_when_in_store(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _json_mode(monkeypatch)
    cfg = _offline_cfg(tmp_path, monkeypatch)
    (cfg.effective_store_dir() / "demo").mkdir(parents=True)
    local_collections.create("pack")

    coll_mod.cmd_collection_add_local(name="pack", skill_slug="demo")
    captured = capsys.readouterr()
    payload = _last_json(captured.out)
    assert payload["in_store"] is True
    assert '"warn"' not in captured.err


def test_cmd_create_local_duplicate_exit1(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    coll_mod.cmd_collection_create_local(name="pack", title=None)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_create_local(name="pack", title=None)


def test_cmd_delete_local_not_found_exit1(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_delete_local(name="ghost")


# ======================================================
#  install-local
# ======================================================
def test_install_local_store_only_no_network(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Чисто-сторовый сценарий: всё в сторе → линк, hub НЕ дёргается вовсе."""
    _json_mode(monkeypatch)
    cfg = _logged_in_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr(
        "skills_hub_cli.core.agents.get_target", lambda name: target
    )
    project = tmp_path / "proj"
    project.mkdir()

    # Материализуем навык в стор (stub-источник — без сети).
    inst = SkillInstaller(target, store_dir=cfg.effective_store_dir())
    inst.install(
        slug="demo", version="1.0.0", commit_sha="a1",
        repo_url=None, manifest=_MANIFEST,
    )

    local_collections.create("pack")
    local_collections.add_skill("pack", "demo")

    hub_calls: list[str] = []

    async def _fake_install_chain(cfg_, access, **kw):  # noqa: ANN001, ANN003
        hub_calls.append(kw.get("slug"))
        return []

    monkeypatch.setattr(coll_mod, "_install_chain", _fake_install_chain)

    coll_mod.cmd_collection_install_local(
        name="pack", scope="project", project=project,
        force=False, channel="published", agent=None,
    )
    payload = _last_json(capsys.readouterr().out)
    assert payload["installed"] == []
    assert payload["skipped"] == []
    assert [it["slug"] for it in payload["linked"]] == ["demo"]
    assert hub_calls == []  # сеть/hub не дёргались
    # Реальный линк в project scope + манифест проекта.
    assert linker.is_link(target.slug_dir("demo", project=project))
    assert "demo" in project_manifest.load(project)


def test_install_local_missing_not_logged_in_skipped(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Слага нет в сторе и не залогинен → skipped с подсказкой, команда НЕ падает."""
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr(
        "skills_hub_cli.core.agents.get_target", lambda name: target
    )
    project = tmp_path / "proj"
    project.mkdir()

    local_collections.create("pack")
    local_collections.add_skill("pack", "ghost")

    coll_mod.cmd_collection_install_local(
        name="pack", scope="project", project=project,
        force=False, channel="published", agent=None,
    )
    payload = _last_json(capsys.readouterr().out)
    assert payload["installed"] == []
    assert payload["linked"] == []
    assert len(payload["skipped"]) == 1
    assert payload["skipped"][0]["slug"] == "ghost"
    assert "залогинен" in payload["skipped"][0]["reason"]
    assert "login" in payload["hint"]


def test_install_local_hub_fallback_when_logged_in(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Слага нет в сторе, залогинен → докачка через общий _install_chain."""
    _json_mode(monkeypatch)
    _logged_in_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr(
        "skills_hub_cli.core.agents.get_target", lambda name: target
    )
    project = tmp_path / "proj"
    project.mkdir()

    local_collections.create("pack")
    local_collections.add_skill("pack", "remote-skill")

    hub_calls: list[str] = []

    async def _fake_install_chain(
        cfg_, access, *, slug, channel, scope, project_path, force, agent_target
    ):  # noqa: ANN001
        hub_calls.append(slug)
        return [{
            "slug": slug, "skill_id": None, "version": "2.0.0",
            "is_update": False, "target_dir": f"/skills/{slug}",
            "scope": scope, "linked": True, "link_kind": "junction",
        }]

    monkeypatch.setattr(coll_mod, "_install_chain", _fake_install_chain)

    coll_mod.cmd_collection_install_local(
        name="pack", scope="project", project=project,
        force=False, channel="published", agent=None,
    )
    payload = _last_json(capsys.readouterr().out)
    assert hub_calls == ["remote-skill"]
    assert [it["slug"] for it in payload["installed"]] == ["remote-skill"]
    assert payload["linked"] == []
    assert payload["skipped"] == []
    assert "remote-skill" in project_manifest.load(project)


def test_install_local_unknown_collection_exit1(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_install_local(
            name="ghost", scope=None, project=None,
            force=False, channel="published", agent=None,
        )


# ======================================================
#  Регистрация: локальные ALWAYS-ON, серверные под гейтом
# ======================================================
_LOCAL_CMDS = {
    "create-local", "delete-local", "add-local",
    "remove-local", "list-local", "install-local",
}


def _collection_cmd_names(app: typer.Typer) -> set[str]:
    group = next(t for t in app.registered_groups if t.name == "collection")
    return {c.name for c in group.typer_instance.registered_commands}


def test_local_commands_registered_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без логина: collection sub-app есть (локальные), серверных команд нет."""
    _offline_cfg(tmp_path, monkeypatch)
    app = main_mod.build_app()
    group_names = {g.name for g in app.registered_groups}
    assert "collection" in group_names
    assert "collections" not in group_names  # серверный list — только при skill.read
    names = _collection_cmd_names(app)
    assert _LOCAL_CMDS <= names
    assert "show" not in names
    assert "install" not in names


def test_server_commands_still_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skill.read без skill.install: show есть, install нет, локальные есть."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    app = main_mod.build_app()
    group_names = {g.name for g in app.registered_groups}
    assert "collections" in group_names
    names = _collection_cmd_names(app)
    assert _LOCAL_CMDS <= names
    assert "show" in names
    assert "install" not in names
