"""Демон-фон: авто-поднятие установленных ХАБ-навыков до latest published хаба.

Баг: `_reconcile_hub_installs` (device-sync между устройствами) берёт целевую
версию из ``installed_version`` (что записано в вебе), а НЕ latest хаба — поэтому
новее опубликованная версия НИКОГДА не поднимается сама. `_auto_update_hub_installs`
закрывает это: тянет latest published (`install_bundle(channel="published")`) и
поднимает ``source=hub`` навыки, если он строго новее локального (`_is_newer`),
scope=global. Гейтится `cfg.auto_update` + cooldown. Не-хабовые (git-url/local)
пропускаются; сбой одного навыка не валит остальные и демон.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from skillery_cli.core.agents import ClaudeCodeTarget


class _FakeHubClient:
    """HubClient-заглушка: install_bundle отдаёт latest по ref из ``bundles``,
    либо бросает ApiError(404), если ref в ``not_in_hub`` (снят/не-хаб)."""

    bundles: dict[str, dict[str, Any]] = {}
    not_in_hub: set[str] = set()

    def __init__(self, *a: Any, **k: Any) -> None:  # noqa: D401
        pass

    async def install_bundle(
        self, slug: str, channel: str = "published"
    ) -> dict[str, Any]:
        from skillery_cli.core.transport import ApiError

        if slug in type(self).not_in_hub:
            raise ApiError(status_code=404, code="NOT_FOUND", message="нет в хабе")
        return dict(type(self).bundles[slug])

    async def close(self) -> None:
        return None


def _seed_skill(store: Path, ref: str, version: str, *, source: str = "hub") -> None:
    d = store / ref
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("hi", encoding="utf-8")
    (d / "_skill_meta.json").write_text(
        json.dumps(
            {"slug": ref, "skill_id": ref, "version": version, "source": source}
        ),
        encoding="utf-8",
    )


def _bundle(version: str, repo_url: str | None = "https://git.example/x.git") -> dict:
    return {
        "version": version,
        "commit_sha": "deadbeef",
        "repo_url": repo_url,
        "manifest": {"version": version, "files": []},
    }


def _wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_update: bool = True,
    bundles: dict[str, dict] | None = None,
    not_in_hub: set[str] | None = None,
) -> tuple[Any, Any, Path, list[dict[str, Any]]]:
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig

    store = tmp_path / "store"
    store.mkdir(exist_ok=True)

    cfg = ClientConfig(store_dir=str(store), base_url="http://localhost:8000")
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    cfg.auto_update = auto_update
    cfg.auto_update_cooldown_min = 60
    cfg.last_auto_update_at = None

    # save() не должен писать реальный конфиг пользователя.
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)

    _FakeHubClient.bundles = bundles or {}
    _FakeHubClient.not_in_hub = not_in_hub or set()
    monkeypatch.setattr(main_mod, "HubClient", _FakeHubClient)

    recorded: list[dict[str, Any]] = []

    async def _fake_install_chain(cfg, access, *, slug, **kwargs):  # noqa: ANN001
        recorded.append({"slug": slug, **kwargs})
        return [{"slug": slug, "version": "x", "scope": kwargs.get("scope")}]

    monkeypatch.setattr(main_mod, "_install_chain", _fake_install_chain)
    return main_mod, cfg, store, recorded


def _run(main_mod, cfg, tmp_path: Path) -> dict[str, list]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    return asyncio.run(
        main_mod._auto_update_hub_installs(
            cfg, "access-tok", agent_target=target, channel="published"
        )
    )


# ---- (a) latest новее локального + auto_update on → поднимаем до latest ----
def test_auto_update_bumps_hub_skill_to_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path, monkeypatch, bundles={"demo": _bundle("0.2.8")}
    )
    _seed_skill(store, "demo", "0.2.2")

    report = _run(main_mod, cfg, tmp_path)

    assert len(recorded) == 1
    assert recorded[0]["slug"] == "demo"
    assert recorded[0]["scope"] == "global"
    assert recorded[0]["project_path"] is None
    assert recorded[0]["channel"] == "published"
    assert report["updated"] == ["demo"]
    # cooldown-таймстамп подвинут после прохода.
    assert cfg.last_auto_update_at is not None


# ---- (b) auto_update off → НЕ поднимаем (device-sync прежний) ----
def test_auto_update_off_does_not_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path, monkeypatch, auto_update=False, bundles={"demo": _bundle("0.2.8")}
    )
    _seed_skill(store, "demo", "0.2.2")

    report = _run(main_mod, cfg, tmp_path)

    assert recorded == []
    assert report["updated"] == []


# ---- (c) source=hub но снят из хаба (install_bundle 404) → skip, демон жив;
#         source!=hub вообще не сканируется ----
def test_auto_update_skips_404_and_non_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path,
        monkeypatch,
        bundles={"demo": _bundle("0.2.8")},
        not_in_hub={"removed"},
    )
    _seed_skill(store, "demo", "0.2.2")
    _seed_skill(store, "removed", "0.2.2")  # source=hub, но 404 в хабе
    _seed_skill(store, "gitskill", "1.0.0", source="git-url")  # не-хаб

    report = _run(main_mod, cfg, tmp_path)

    # demo обновлён; removed мягко пропущен (не упали); gitskill не сканирован.
    assert [r["slug"] for r in recorded] == ["demo"]
    assert report["updated"] == ["demo"]
    assert "removed" in report["skipped"]
    assert "gitskill" not in report["updated"]
    assert "gitskill" not in report["skipped"]


# ---- (d) уже latest → skip, без обновления ----
def test_auto_update_skips_when_already_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path, monkeypatch, bundles={"demo": _bundle("1.0.0")}
    )
    _seed_skill(store, "demo", "1.0.0")

    report = _run(main_mod, cfg, tmp_path)

    assert recorded == []
    assert report["updated"] == []
    assert report["skipped"] == ["demo"]


# ---- (e) latest СТАРШЕ локального → НЕ даунгрейдим ----
def test_auto_update_no_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path, monkeypatch, bundles={"demo": _bundle("1.0.0")}
    )
    _seed_skill(store, "demo", "2.0.0")

    report = _run(main_mod, cfg, tmp_path)

    assert recorded == []
    assert report["updated"] == []
    assert report["skipped"] == ["demo"]


# ---- cooldown: свежий last_auto_update_at → ничего не делаем ----
def test_auto_update_respects_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path, monkeypatch, bundles={"demo": _bundle("0.2.8")}
    )
    _seed_skill(store, "demo", "0.2.2")
    # Апдейт был только что → в пределах 60-мин cooldown, пропускаем.
    cfg.last_auto_update_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    report = _run(main_mod, cfg, tmp_path)

    assert recorded == []
    assert report["updated"] == []


# ---- stub-источник (bundle без repo_url) → skip (обновлять нечем) ----
def test_auto_update_skips_stub_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path, monkeypatch, bundles={"demo": _bundle("0.2.8", repo_url=None)}
    )
    _seed_skill(store, "demo", "0.2.2")

    report = _run(main_mod, cfg, tmp_path)

    assert recorded == []
    assert report["updated"] == []
    assert report["skipped"] == ["demo"]


# ---- одна упавшая установка (_install_chain кинул) не валит остальные ----
def test_auto_update_isolates_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(
        tmp_path,
        monkeypatch,
        bundles={"a": _bundle("2.0.0"), "b": _bundle("2.0.0")},
    )
    _seed_skill(store, "a", "1.0.0")
    _seed_skill(store, "b", "1.0.0")

    async def _flaky_install_chain(cfg, access, *, slug, **kwargs):  # noqa: ANN001
        if slug == "a":
            raise RuntimeError("git clone blew up")
        recorded.append({"slug": slug, **kwargs})
        return [{"slug": slug}]

    monkeypatch.setattr(main_mod, "_install_chain", _flaky_install_chain)

    report = _run(main_mod, cfg, tmp_path)

    assert "a" in report["failed"]
    assert report["updated"] == ["b"]
    assert [r["slug"] for r in recorded] == ["b"]


# ---- нет хаб-навыков в сторе → no-op, cooldown двигается ----
def test_auto_update_no_hub_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_mod, cfg, store, recorded = _wire(tmp_path, monkeypatch, bundles={})
    _seed_skill(store, "gitskill", "1.0.0", source="git-url")

    report = _run(main_mod, cfg, tmp_path)

    assert recorded == []
    assert report == {"updated": [], "skipped": [], "failed": []}
    assert cfg.last_auto_update_at is not None


# ---- Демон вызывает ВСЕ проходы тяжёлой сверки ----
# #1490: третьим встал такт лизов способностей. Проверяется здесь, а не
# отдельным тестом, потому что вопрос ровно один — из чего состоит тяжёлый
# такт; два ответа на него разъехались бы при следующей правке.
@pytest.mark.asyncio
async def test_daemon_reconcile_runs_both_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skillery_cli.__main__ as main_mod
    import skillery_cli.commands.daemon as daemon_mod
    import skillery_cli.config as config_mod
    import skillery_cli.core.agents as agents_mod
    from skillery_cli.config import ClientConfig

    cfg = ClientConfig(store_dir=str(tmp_path / "store"))
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        config_mod, "load_tokens", lambda email: ("access-tok", "refresh-tok")
    )
    monkeypatch.setattr(agents_mod, "get_target", lambda name: object())

    calls: list[str] = []

    async def _fake_reconcile(cfg, access, **kwargs):  # noqa: ANN001
        calls.append("device-sync")
        return {"downloaded": [], "updated": [], "skipped": [], "failed": []}

    async def _fake_auto_update(cfg, access, **kwargs):  # noqa: ANN001
        calls.append("auto-update-latest")
        return {"updated": [], "skipped": [], "failed": []}

    monkeypatch.setattr(main_mod, "_reconcile_hub_installs", _fake_reconcile)
    monkeypatch.setattr(main_mod, "_auto_update_hub_installs", _fake_auto_update)
    # #1102: каденс self-upgrade не должен ходить в PyPI в этом тесте — глушим.
    async def _no_self_upgrade(cfg, *, force):  # noqa: ANN001
        return False

    monkeypatch.setattr(main_mod, "_daemon_cli_self_upgrade", _no_self_upgrade)

    async def _fake_leases(cfg, access):  # noqa: ANN001
        calls.append("capability-leases")
        return {"granted": 0}

    monkeypatch.setattr(main_mod, "_reconcile_capability_leases", _fake_leases)

    runner = daemon_mod._build_runner(interval_seconds=60)
    assert runner._reconcile is not None
    await runner._reconcile()

    assert calls == ["device-sync", "auto-update-latest", "capability-leases"]
