"""Тесты ``skills-hub collection install <ID_ИЛИ_SLUG>`` (серверная массовая установка).

Главный онбординг-кейс: ставит ВСЕ effective skills коллекции через общий
``_install_chain`` из ``__main__`` (тот же путь что ``install``). Серверный режим
включается RBAC-флагами ``_SERVER_ENABLED`` (skill.read) + ``_CAN_INSTALL``
(skill.install); локальный — флагом ``--local`` (см. test_p1_local_collections).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from skills_hub_cli import output as output_module
from skills_hub_cli.commands import _common, collection as coll_mod
from skills_hub_cli.config import ClientConfig


def _text_mode() -> None:
    output_module._mode = "text"


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


def _logged_in_cfg(monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "skill.install"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))
    # Серверный режим коллекций (как выставил бы register по правам JWT).
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", True)
    monkeypatch.setattr(coll_mod, "_CAN_INSTALL", True)
    return cfg


def test_cmd_collection_install_installs_each_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _text_mode()
    _logged_in_cfg(monkeypatch)

    fake_client = MagicMock()

    async def _get(slug: str) -> dict[str, Any]:
        return {
            "collection": {
                "id": "col_1", "slug": slug, "title": "Onboarding",
                "type": "static", "skills_count": 2,
            },
            "skills": [
                {"id": "10", "slug": "skill-a", "title": "Skill A"},
                {"id": "11", "slug": "skill-b", "title": "Skill B"},
            ],
            "tags": [],
        }

    async def _close() -> None:
        return None

    fake_client.get_collection = _get
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    installed_slugs: list[str] = []

    async def _fake_install_chain(cfg, access, *, slug, channel, scope, project_path, force, agent_target):  # noqa: ANN001
        installed_slugs.append(slug)
        return [{
            "slug": slug, "skill_id": None, "version": "1.0.0", "is_update": False,
            "target_dir": f"/skills/{slug}", "scope": scope,
            "linked": True, "link_kind": "symlink",
        }]

    monkeypatch.setattr(coll_mod, "_install_chain", _fake_install_chain)

    coll_mod.cmd_collection_install(
        ref="onboarding", local=False, scope=None, project=None,
        channel="published", force=False, agent=None,
    )
    assert installed_slugs == ["skill-a", "skill-b"]


def test_cmd_collection_install_uses_id_when_slug_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slug-less skill (PK migration) ставится по числовому id."""
    _text_mode()
    _logged_in_cfg(monkeypatch)

    fake_client = MagicMock()

    async def _get(slug: str) -> dict[str, Any]:
        return {
            "collection": {"id": "col_1", "slug": slug, "title": "C", "type": "static"},
            "skills": [{"id": "42", "slug": None, "title": "Slugless"}],
            "tags": [],
        }

    async def _close() -> None:
        return None

    fake_client.get_collection = _get
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    refs: list[str] = []

    async def _fake_install_chain(cfg, access, *, slug, channel, scope, project_path, force, agent_target):  # noqa: ANN001
        refs.append(slug)
        return [{"slug": slug, "skill_id": "42", "version": "1.0.0", "is_update": False,
                 "target_dir": "/skills/42", "scope": scope, "linked": True, "link_kind": "symlink"}]

    monkeypatch.setattr(coll_mod, "_install_chain", _fake_install_chain)

    coll_mod.cmd_collection_install(
        ref="c", local=False, scope=None, project=None,
        channel="published", force=False, agent=None,
    )
    assert refs == ["42"]


def test_cmd_collection_install_respects_scope_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _text_mode()
    _logged_in_cfg(monkeypatch)

    fake_client = MagicMock()

    async def _get(slug: str) -> dict[str, Any]:
        return {
            "collection": {"id": "col_1", "slug": slug, "title": "C", "type": "static"},
            "skills": [{"id": "10", "slug": "skill-a", "title": "A"}],
            "tags": [],
        }

    async def _close() -> None:
        return None

    fake_client.get_collection = _get
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    captured_scope: dict[str, Any] = {}

    async def _fake_install_chain(cfg, access, *, slug, channel, scope, project_path, force, agent_target):  # noqa: ANN001
        captured_scope["scope"] = scope
        captured_scope["project_path"] = project_path
        return [{"slug": slug, "skill_id": None, "version": "1.0.0", "is_update": False,
                 "target_dir": "/x", "scope": scope, "linked": True, "link_kind": "symlink"}]

    monkeypatch.setattr(coll_mod, "_install_chain", _fake_install_chain)

    coll_mod.cmd_collection_install(
        ref="c", local=False, scope="project", channel="published",
        force=False, project=tmp_path, agent=None,
    )
    assert captured_scope["scope"] == "project"
    assert captured_scope["project_path"] is not None


def test_cmd_collection_install_empty_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустая коллекция — не падает, ничего не ставит."""
    _text_mode()
    _logged_in_cfg(monkeypatch)

    fake_client = MagicMock()

    async def _get(slug: str) -> dict[str, Any]:
        return {
            "collection": {"id": "col_1", "slug": slug, "title": "Empty", "type": "static"},
            "skills": [], "tags": [],
        }

    async def _close() -> None:
        return None

    fake_client.get_collection = _get
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    calls: list[str] = []

    async def _fake_install_chain(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        calls.append("called")
        return []

    monkeypatch.setattr(coll_mod, "_install_chain", _fake_install_chain)

    coll_mod.cmd_collection_install(
        ref="empty", local=False, scope=None, project=None,
        channel="published", force=False, agent=None,
    )
    assert calls == []  # install_chain не звонился


def test_collection_install_show_always_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Единый sub-app: install/show зарегистрированы ВСЕГДА (есть локальный режим);
    серверный режим включают флаги _SERVER_ENABLED/_CAN_INSTALL по правам."""
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", False)
    monkeypatch.setattr(coll_mod, "_CAN_INSTALL", False)
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read", "skill.install"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    collection_group = next(t for t in app.registered_groups if t.name == "collection")
    sub_names = [c.name for c in collection_group.typer_instance.registered_commands]
    assert "install" in sub_names
    assert "show" in sub_names
    # skill.read + skill.install → серверный режим И установка включены.
    assert coll_mod._SERVER_ENABLED is True
    assert coll_mod._CAN_INSTALL is True


def test_collection_install_gated_without_skill_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skill.read без skill.install: команда есть, но _CAN_INSTALL=False
    (серверный install в рантайме даст NOT_AVAILABLE)."""
    monkeypatch.setattr(coll_mod, "_CAN_INSTALL", True)
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    collection_group = next(t for t in app.registered_groups if t.name == "collection")
    sub_names = [c.name for c in collection_group.typer_instance.registered_commands]
    assert "install" in sub_names
    assert coll_mod._SERVER_ENABLED is True
    assert coll_mod._CAN_INSTALL is False
