"""#2282 — обновление ЗАВИСИМОСТИ доезжает до потребителя.

Живой разбор. ``skill update`` шёл по установленным навыкам поштучно и сравнивал
ТОЛЬКО версию самого навыка: если у потребителя версия не менялась, он получал
«актуально», а новая версия объявленной им зависимости (или зависимость,
появившаяся в новой версии манифеста) не приезжала вовсе. Плюс обновление
ставило навык напрямую по ``repo_url``, минуя цепочку — то есть ветка «навык
тянет другой навык» в апдейте не участвовала.

Здесь закреплено: если хоть один элемент ``dependencies_chain`` локально
отсутствует или старее — навык обновляется ЧЕРЕЗ цепочку.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli.core.agents import ClaudeCodeTarget


def _install_skill(target: ClaudeCodeTarget, slug: str, version: str) -> None:
    d = target.slug_dir(slug)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("hi", encoding="utf-8")
    (d / "_skill_meta.json").write_text(
        json.dumps({"slug": slug, "version": version}), encoding="utf-8"
    )


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, chain, self_version):
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    _install_skill(target, "hello-consumer", "1.0.0")
    _install_skill(target, "hello-base", "1.0.0")

    cfg = ClientConfig(store_dir=str(tmp_path / "store"), base_url="http://x")
    cfg.user_email = "x@y.io"
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_apply_tooling", lambda *a, **k: None)

    bundles = {
        "hello-consumer": {
            "skill_slug": "hello-consumer",
            "version": self_version,
            "commit_sha": "deadbeef",
            "repo_url": None,
            "manifest": {"version": self_version, "files": []},
            "dependencies_chain": chain,
        },
        "hello-base": {
            "skill_slug": "hello-base",
            "version": "1.1.0",
            "commit_sha": "cafe",
            "repo_url": None,
            "manifest": {"version": "1.1.0", "files": []},
            "dependencies_chain": [["hello-base", "1.1.0", None]],
        },
    }

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            return bundles[slug]

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)

    installed_via_chain: list[dict] = []

    async def _fake_chain(cfg_, access, **kw):  # type: ignore[no-untyped-def]
        installed_via_chain.append(kw)
        rows = []
        for dep_slug, dep_version, _repo in bundles[kw["slug"]]["dependencies_chain"]:
            rows.append(
                {
                    "slug": dep_slug,
                    "skill_id": None,
                    "version": dep_version,
                    "is_update": True,
                    "target_dir": str(target.slug_dir(dep_slug)),
                    "scope": "global",
                    "linked": True,
                    "link_kind": "link",
                }
            )
        return rows

    monkeypatch.setattr(main_mod, "_install_chain", _fake_chain)

    captured: dict = {}
    monkeypatch.setattr(
        main_mod, "emit_data", lambda data, **kw: captured.update({"rows": data})
    )
    return main_mod, captured, installed_via_chain


def test_new_dependency_version_reaches_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Версия ПОТРЕБИТЕЛЯ не менялась, но зависимость вышла новее — обновляем."""
    main_mod, captured, via_chain = _prepare(
        tmp_path,
        monkeypatch,
        chain=[["hello-base", "1.1.0", None], ["hello-consumer", "1.0.0", None]],
        self_version="1.0.0",
    )
    main_mod.cmd_update(
        slug="hello-consumer", all_=False, channel="published", project=None,
        scope="global",
    )
    assert via_chain, "цепочка не переустановлена — обновление зависимости потеряно"
    rows = captured.get("rows") or []
    base_rows = [r for r in rows if r.get("slug") == "hello-base"]
    assert base_rows and base_rows[0]["to"] == "1.1.0"
    assert base_rows[0]["updated"] is True
    # «from» снимается ДО установки — иначе отчёт показывал бы 1.1.0 → 1.1.0.
    assert base_rows[0]["from"] == "1.0.0"


def test_chain_up_to_date_reports_no_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Всё совпадает — «актуально», цепочка НЕ переустанавливается."""
    main_mod, captured, via_chain = _prepare(
        tmp_path,
        monkeypatch,
        chain=[["hello-base", "1.0.0", None], ["hello-consumer", "1.0.0", None]],
        self_version="1.0.0",
    )
    main_mod.cmd_update(
        slug="hello-consumer", all_=False, channel="published", project=None,
        scope="global",
    )
    assert via_chain == []
    rows = captured.get("rows") or []
    assert rows and all(not r.get("updated") for r in rows)


def test_missing_dependency_is_installed_on_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Зависимость появилась в новой версии манифеста — её нет локально."""
    main_mod, captured, via_chain = _prepare(
        tmp_path,
        monkeypatch,
        chain=[
            ["hello-base", "1.0.0", None],
            ["brand-new-dep", "1.0.0", None],
            ["hello-consumer", "1.0.0", None],
        ],
        self_version="1.0.0",
    )
    main_mod.cmd_update(
        slug="hello-consumer", all_=False, channel="published", project=None,
        scope="global",
    )
    assert via_chain, "новая зависимость не поставлена при обновлении"
    rows = captured.get("rows") or []
    assert any(r.get("slug") == "brand-new-dep" for r in rows)
