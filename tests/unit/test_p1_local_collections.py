"""Тесты P1 C4 — локальные коллекции БЕЗ хаба (E10), единый ``--local`` API.

Покрытие:
- core/local_collections.py: CRUD поверх <config_dir>/collections.toml
  (уважает SKILLERY_CONFIG_DIR), идемпотентность add/remove, валидация имён;
- commands/collection.py: единый sub-app ``collection`` с глаголами
  list/show/install/create/add/remove/delete + флаг ``--local``;
- ``--local`` режим — оффлайн CRUD без логина, warning при добавлении слага,
  которого нет в сторе; install --local: store → link без сети, отсутствующий
  слаг без логина → skipped (команда не падает), hub-докачка когда залогинен;
- гейтинг: create/add/remove/delete без ``--local`` → ошибка USE_LOCAL_FLAG;
  list/show/install без ``--local`` и без skill.read → ошибка NOT_AVAILABLE.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

import skillery_cli.__main__ as main_mod
import skillery_cli.config as config_module
from skillery_cli import output as out_mod
from skillery_cli.commands import _common, collection as coll_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import linker, local_collections, project_manifest
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


@pytest.fixture()
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный config-dir: collections.toml живёт в tmp, профиль сброшен."""
    d = tmp_path / "cfg"
    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(d))
    monkeypatch.delenv("SKILLERY_PROFILE", raising=False)
    monkeypatch.setattr(config_module, "_ACTIVE_PROFILE", None)
    return d


def _json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(out_mod, "_mode", "json")


def _last_json(out: str) -> dict | list:
    return json.loads(out.strip().splitlines()[-1])


def _offline_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
    """Незалогиненный конфиг со стором в tmp."""
    cfg = ClientConfig(store_dir=str(tmp_path / "store"))
    assert cfg.is_logged_in() is False
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    return cfg


def _logged_in_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
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
    path = local_collections.collections_path()
    assert path == cfg_dir / "collections.toml"
    assert path.exists()
    loaded = local_collections.load_all()
    assert loaded["web-pack"]["title"] == "Web навыки"
    assert loaded["web-pack"]["skills"] == []


def test_create_default_title_is_name(cfg_dir: Path) -> None:
    assert local_collections.create("pack")["title"] == "pack"


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
    assert local_collections.missing_in_store(["have", "ghost"], store) == ["ghost"]


# ======================================================
#  Команды --local — оффлайн, без логина и сети
# ======================================================
def test_cmd_crud_local_offline(
    cfg_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)

    coll_mod.cmd_collection_create(name="pack", local=True, title="Мой набор")
    payload = _last_json(capsys.readouterr().out)
    assert payload["event"] == "local_collection_created"
    assert payload["name"] == "pack"
    assert payload["title"] == "Мой набор"

    # add --local: слага нет в сторе → warning в stderr, но добавлен всё равно.
    coll_mod.cmd_collection_add(name="pack", skill_slug="demo", local=True)
    captured = capsys.readouterr()
    payload = _last_json(captured.out)
    assert payload["added"] is True
    assert payload["in_store"] is False
    assert payload["skills"] == ["demo"]
    assert '"warn"' in captured.err

    # list --local: count + missing_in_store.
    coll_mod.cmd_collection_list(local=True)
    rows = _last_json(capsys.readouterr().out)
    assert rows == [
        {
            "name": "pack", "title": "Мой набор", "skills_count": 1,
            "skills": ["demo"], "missing_in_store": ["demo"],
        }
    ]

    # show --local: детали одной локальной коллекции.
    coll_mod.cmd_collection_show(ref="pack", local=True)
    payload = _last_json(capsys.readouterr().out)
    assert payload["name"] == "pack"
    assert payload["skills"] == ["demo"]
    assert payload["missing_in_store"] == ["demo"]

    # remove --local.
    coll_mod.cmd_collection_remove(name="pack", skill_slug="demo", local=True)
    payload = _last_json(capsys.readouterr().out)
    assert payload["removed"] is True
    assert payload["skills"] == []

    # delete --local.
    coll_mod.cmd_collection_delete(name="pack", local=True)
    payload = _last_json(capsys.readouterr().out)
    assert payload["event"] == "local_collection_deleted"
    assert local_collections.load_all() == {}


def test_cmd_add_local_no_warning_when_in_store(
    cfg_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _json_mode(monkeypatch)
    cfg = _offline_cfg(tmp_path, monkeypatch)
    (cfg.effective_store_dir() / "demo").mkdir(parents=True)
    local_collections.create("pack")

    coll_mod.cmd_collection_add(name="pack", skill_slug="demo", local=True)
    captured = capsys.readouterr()
    payload = _last_json(captured.out)
    assert payload["in_store"] is True
    assert '"warn"' not in captured.err


def test_cmd_create_local_duplicate_exit1(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    coll_mod.cmd_collection_create(name="pack", local=True, title=None)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_create(name="pack", local=True, title=None)


def test_cmd_delete_local_not_found_exit1(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_delete(name="ghost", local=True)


# ======================================================
#  Гейтинг флага --local
# ======================================================
@pytest.mark.parametrize(
    "call",
    [
        lambda: coll_mod.cmd_collection_create(name="x", local=False, title=None),
        lambda: coll_mod.cmd_collection_add(name="x", skill_slug="s", local=False),
        lambda: coll_mod.cmd_collection_remove(name="x", skill_slug="s", local=False),
    ],
)
def test_server_crud_without_manage_perm_errors(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], call,  # noqa: ANN001
) -> None:
    """D-CLI M-2: create/add/remove без --local и без catalog.manage →
    NOT_AVAILABLE (серверный CRUD теперь есть, но гейтится правом)."""
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(coll_mod, "_CAN_MANAGE", False)
    with pytest.raises(typer.Exit):
        call()
    evt = _last_json(capsys.readouterr().err)
    assert evt["code"] == "NOT_AVAILABLE"


def test_server_delete_not_supported_in_cli(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D-CLI M-2: серверный delete из CLI не поддержан (только Web UI) →
    USE_LOCAL_FLAG-подсказка про --local."""
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_delete(name="x", local=False)
    evt = _last_json(capsys.readouterr().err)
    assert evt["code"] == "USE_LOCAL_FLAG"


def test_list_server_verb_without_login_errors(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """list без --local при выключенном серверном режиме → NOT_AVAILABLE."""
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", False)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_list(local=False)
    evt = _last_json(capsys.readouterr().err)
    assert evt["code"] == "NOT_AVAILABLE"


def test_install_server_verb_without_install_perm_errors(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """install без --local при skill.read но без skill.install → NOT_AVAILABLE."""
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", True)
    monkeypatch.setattr(coll_mod, "_CAN_INSTALL", False)
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_install(
            ref="x", local=False, scope=None, project=None,
            channel="published", force=False, agent=None,
        )
    evt = _last_json(capsys.readouterr().err)
    assert evt["code"] == "NOT_AVAILABLE"


# ======================================================
#  install --local
# ======================================================
def test_install_local_store_only_no_network(
    cfg_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Чисто-сторовый сценарий: всё в сторе → линк, hub НЕ дёргается вовсе."""
    _json_mode(monkeypatch)
    cfg = _logged_in_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr("skillery_cli.core.agents.get_target", lambda name: target)
    project = tmp_path / "proj"
    project.mkdir()

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

    coll_mod.cmd_collection_install(
        ref="pack", local=True, scope="project", project=project,
        force=False, channel="published", agent=None,
    )
    payload = _last_json(capsys.readouterr().out)
    assert payload["installed"] == []
    assert payload["skipped"] == []
    assert [it["slug"] for it in payload["linked"]] == ["demo"]
    assert hub_calls == []  # сеть/hub не дёргались
    assert linker.is_link(target.slug_dir("demo", project=project))
    assert "demo" in project_manifest.load(project)


def test_install_local_missing_not_logged_in_skipped(
    cfg_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Слага нет в сторе и не залогинен → skipped с подсказкой, команда НЕ падает."""
    _json_mode(monkeypatch)
    _offline_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr("skillery_cli.core.agents.get_target", lambda name: target)
    project = tmp_path / "proj"
    project.mkdir()

    local_collections.create("pack")
    local_collections.add_skill("pack", "ghost")

    coll_mod.cmd_collection_install(
        ref="pack", local=True, scope="project", project=project,
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
    cfg_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Слага нет в сторе, залогинен → докачка через общий _install_chain."""
    _json_mode(monkeypatch)
    _logged_in_cfg(tmp_path, monkeypatch)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr("skillery_cli.core.agents.get_target", lambda name: target)
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

    coll_mod.cmd_collection_install(
        ref="pack", local=True, scope="project", project=project,
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
        coll_mod.cmd_collection_install(
            ref="ghost", local=True, scope=None, project=None,
            force=False, channel="published", agent=None,
        )


# ======================================================
#  Регистрация: единый sub-app collection, 8 глаголов, без plural
#  (D-CLI M-2 добавил серверный `tags`).
# ======================================================
_VERBS = {
    "list", "show", "install", "create", "add", "remove", "delete", "tags",
}


def _collection_cmd_names(app: typer.Typer) -> set[str]:
    group = next(t for t in app.registered_groups if t.name == "collection")
    return {c.name for c in group.typer_instance.registered_commands}


def test_single_collection_subapp_all_verbs_no_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без логина: единый collection sub-app со всеми 7 глаголами; нет plural."""
    _offline_cfg(tmp_path, monkeypatch)
    app = main_mod.build_app()
    group_names = {g.name for g in app.registered_groups}
    assert "collection" in group_names
    assert "collections" not in group_names  # plural убран — единый глагол list
    assert _collection_cmd_names(app) == _VERBS


def test_register_sets_server_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """register прокидывает RBAC в модульные флаги серверного режима."""
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", False)
    monkeypatch.setattr(coll_mod, "_CAN_INSTALL", False)
    app = typer.Typer()
    coll_mod.register(app, server_enabled=True, can_install=True)
    assert coll_mod._SERVER_ENABLED is True
    assert coll_mod._CAN_INSTALL is True
    # И обратно — без прав серверный режим выключен.
    coll_mod.register(typer.Typer(), server_enabled=False, can_install=False)
    assert coll_mod._SERVER_ENABLED is False
