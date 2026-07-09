"""`skillery pull`: докачка навыков, помеченных установленными в вебе.

Поток «нажал Установить в вебе → CLI скачал»:
- ``GET /me/installs`` → для каждого отсутствующего/устаревшего навыка
  ``_install_chain`` (скачивание + материализация в стор);
- уже актуальный (та же версия в сторе) — пропускается (идемпотентность).

Транспорт и инсталлер мокаются (без сети/git).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from skillery_cli.core.agents import ClaudeCodeTarget


class _FakeHubClient:
    """Заглушка HubClient: отдаёт заранее заданный /me/installs."""

    installs: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        pass

    async def list_my_installs(self) -> list[dict[str, Any]]:
        return list(type(self).installs)

    async def close(self) -> None:
        return None


def _seed_store(store: Path, name: str, version: str) -> None:
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("hi", encoding="utf-8")
    (d / "_skill_meta.json").write_text(
        json.dumps({"slug": name, "version": version}), encoding="utf-8"
    )


def _wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    installs: list[dict[str, Any]],
) -> tuple[Any, list[dict[str, Any]]]:
    """Поднять cfg+target+моки; вернуть (main_mod, recorded_install_calls)."""
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig
    from skillery_cli import output as out_mod

    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    target = ClaudeCodeTarget(root=tmp_path / ".claude")

    cfg = ClientConfig(store_dir=str(store))
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "access-tok")
    monkeypatch.setattr(out_mod, "_mode", "json")

    _FakeHubClient.installs = installs
    monkeypatch.setattr(main_mod, "HubClient", _FakeHubClient)

    recorded: list[dict[str, Any]] = []

    async def _fake_install_chain(cfg, access, *, slug, **kwargs):  # noqa: ANN001
        recorded.append({"slug": slug, **kwargs})
        # Симулируем материализацию в стор (как сделал бы реальный installer).
        _seed_store(Path(cfg.store_dir), slug, "1.0.0")
        return [{"slug": slug, "version": "1.0.0", "scope": "global"}]

    monkeypatch.setattr(main_mod, "_install_chain", _fake_install_chain)
    return main_mod, recorded


def test_pull_downloads_missing_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    main_mod, recorded = _wire(
        tmp_path,
        monkeypatch,
        installs=[
            {"slug": "bitrix24", "skill_id": "10", "installed_version": "1.0.0"}
        ],
    )

    main_mod.cmd_pull(agent=None, channel="published", force=False)

    # Навыка не было в сторе → скачан ровно один раз (global scope).
    assert len(recorded) == 1
    assert recorded[0]["slug"] == "bitrix24"
    assert recorded[0]["scope"] == "global"
    assert recorded[0]["project_path"] is None
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "bitrix24" in payload["downloaded"]


def test_pull_skips_already_installed_same_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    main_mod, recorded = _wire(
        tmp_path,
        monkeypatch,
        installs=[
            {"slug": "bitrix24", "skill_id": "10", "installed_version": "1.0.0"}
        ],
    )
    # Уже в сторе той же версии → pull пропускает (НЕ качает).
    _seed_store(tmp_path / "store", "bitrix24", "1.0.0")

    main_mod.cmd_pull(agent=None, channel="published", force=False)

    assert recorded == []
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "bitrix24" in payload["skipped"]
    assert payload["downloaded"] == []


def test_pull_updates_when_remote_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    main_mod, recorded = _wire(
        tmp_path,
        monkeypatch,
        installs=[
            {"slug": "bitrix24", "skill_id": "10", "installed_version": "2.0.0"}
        ],
    )
    # Локально 1.0.0, в вебе помечена 2.0.0 → докачиваем (update).
    _seed_store(tmp_path / "store", "bitrix24", "1.0.0")

    main_mod.cmd_pull(agent=None, channel="published", force=False)

    assert len(recorded) == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "bitrix24" in payload["updated"]


def test_pull_slugless_uses_skill_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """slug=None (демотирован) → навык адресуется по skill_id."""
    main_mod, recorded = _wire(
        tmp_path,
        monkeypatch,
        installs=[
            {"slug": None, "skill_id": "42", "installed_version": "1.0.0"}
        ],
    )

    main_mod.cmd_pull(agent=None, channel="published", force=False)

    assert len(recorded) == 1
    assert recorded[0]["slug"] == "42"
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "42" in payload["downloaded"]


def test_reconcile_helper_returns_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_reconcile_hub_installs` напрямую: переиспользуется демоном."""
    main_mod, recorded = _wire(
        tmp_path,
        monkeypatch,
        installs=[
            {"slug": "a", "skill_id": "1", "installed_version": "1.0.0"},
            {"slug": "b", "skill_id": "2", "installed_version": "1.0.0"},
        ],
    )
    from skillery_cli.config import ClientConfig
    from skillery_cli.core.agents import get_target  # noqa: F401

    cfg = ClientConfig.load()
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    report = asyncio.run(
        main_mod._reconcile_hub_installs(
            cfg, "access-tok", channel="published", agent_target=target,
        )
    )
    assert sorted(report["downloaded"]) == ["a", "b"]
    assert len(recorded) == 2
