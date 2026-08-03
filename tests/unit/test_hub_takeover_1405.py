"""#1405 — установка из Хаба становится ГЛАВНОЙ, не унося данные пользователя.

СИМПТОМ С ЖИВОЙ МАШИНЫ ВЛАДЕЛЬЦА. Навык установлен через веб, интерфейс
отрапортовал успех, а ``skillery installed`` показывает ``local-path`` — и при
вызове исполняется ЛОКАЛЬНАЯ версия. Здесь закреплён итоговый контракт:

* хабовая установка поверх локальной делает главной хабовую (``source=hub``);
* прежняя версия НЕ затирается: она целиком уходит в резерв внутри стора и
  возвращается командой отката;
* данные навыка переживают замену — и те, что лежат СНАРУЖИ каталога навыка
  (как ``~/.atlas/atlas.db``), и те, что лежат ВНУТРИ (``.env``, ``_local/``);
* ``installed`` и ``list --installed`` отвечают из ОДНОГО источника правды.

Проверяется на РЕАЛЬНОМ ките (``SkillInstaller`` + настоящий tar.gz-снапшот) —
фейк установщика здесь бесполезен: баг был ровно в том, какую метку кит пишет
в мету при разных способах вызова.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from skillery_cli import __main__ as m
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller, read_meta
from skillery_cli.core.store_backup import list_backups, restore_backup

_HUB_VERSION = "2.0.0"
_LOCAL_VERSION = "0.3.9"


def _snapshot(body: str) -> bytes:
    """tar.gz репозитория с навыком в КОРНЕ (как раздаёт бэкенд)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, payload in [
            ("repo-abc/SKILL.md", body.encode("utf-8")),
            ("repo-abc/hub_only.txt", b"from hub"),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            t.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _FakeHubClient:
    """Ровно те методы, которые зовёт ``_install_chain``."""

    def __init__(self, snap: bytes, **_kw) -> None:
        self._snap = snap

    async def install_bundle(self, slug: str, *, channel: str = "published") -> dict:
        return {
            "skill_slug": slug,
            "version": _HUB_VERSION,
            "repo_url": "https://git.example/x.git",
            "commit_sha": "deadbeef",
            "skill_path": None,
            "skill_id": 42,
            "manifest": {"version": _HUB_VERSION, "files": []},
        }

    async def download_snapshot(self, ref: str, semver: str) -> bytes:
        return self._snap

    async def close(self) -> None:
        return None


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Стор/таргет на tmp, побочные эффекты установки заглушены."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    store.mkdir()

    cfg = ClientConfig(store_dir=str(store))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(m, "get_target", lambda name: target, raising=False)
    monkeypatch.setattr(m, "track_skill_event", lambda et, **kw: None, raising=False)
    monkeypatch.setattr(m, "_apply_tooling", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(m, "_emit_onboarding", lambda *a, **kw: None, raising=False)
    snap = _snapshot(f"---\nname: demo\nversion: {_HUB_VERSION}\n---\n\n# demo (ХАБ)\n")
    monkeypatch.setattr(m, "HubClient", lambda **kw: _FakeHubClient(snap), raising=False)
    return cfg, target, store, tmp_path


def _install_local(target, store: Path, src_root: Path) -> Path:
    """Локальная установка навыка ``demo`` (как ``skillery install --path``)."""
    src = src_root / "demo-src"
    (src).mkdir()
    (src / "SKILL.md").write_text(
        f"---\nname: demo\nversion: {_LOCAL_VERSION}\n---\n\n# demo (ЛОКАЛЬНЫЙ)\n",
        encoding="utf-8",
    )
    SkillInstaller(target, store_dir=store).install(
        slug="demo", version=_LOCAL_VERSION, commit_sha="", repo_url=None,
        local_src=src, manifest={"version": _LOCAL_VERSION, "files": []},
        project=None,
    )
    return store / "demo"


async def _install_from_hub(cfg, target) -> list[dict]:
    return await m._install_chain(
        cfg, "token", slug="demo", channel="published", scope="global",
        project_path=None, force=False, agent_target=target, headless=True,
    )


class TestHubBeatsLocal:
    async def test_hub_install_becomes_active(self, env) -> None:
        """(а) Хабовая версия становится главной поверх локальной."""
        cfg, target, store, tmp = env
        _install_local(target, store, tmp)
        assert read_meta(store / "demo")["source"] == "local-path"

        await _install_from_hub(cfg, target)

        meta = read_meta(store / "demo")
        assert meta["source"] == "hub", "хаб обязан вытеснить локальную установку"
        assert meta["version"] == _HUB_VERSION
        assert "ХАБ" in (store / "demo" / "SKILL.md").read_text(encoding="utf-8")
        # Контент приехал целиком, а не патчем поверх локального дерева.
        assert (store / "demo" / "hub_only.txt").is_file()

    async def test_previous_version_backed_up_and_restorable(self, env) -> None:
        """(б) Прежняя версия сохранена в резерв, и её можно вернуть."""
        cfg, target, store, tmp = env
        _install_local(target, store, tmp)

        chain = await _install_from_hub(cfg, target)

        # Факт вытеснения виден в ответе команды (веб/скрипты).
        assert chain[0]["replaced"]["source"] == "local-path"
        assert chain[0]["replaced"]["version"] == _LOCAL_VERSION

        backups = list_backups(store, "demo")
        assert len(backups) == 1
        saved = Path(backups[0]["skill_path"]) / "SKILL.md"
        assert "ЛОКАЛЬНЫЙ" in saved.read_text(encoding="utf-8")

        restore_backup(store, "demo", backups[0]["id"])

        meta = read_meta(store / "demo")
        assert meta["source"] == "local-path", "откат вернул прежнюю версию"
        assert meta["version"] == _LOCAL_VERSION
        # Откат сам обратим: вытесненная хаб-версия не удалена, а зарезервирована.
        assert any(b["source"] == "hub" for b in list_backups(store, "demo"))

    async def test_backup_is_not_a_skill_and_survives_gc(self, env) -> None:
        """Служебная зона резервов не выдаётся за навык (иначе её снёс бы gc)."""
        from skillery_cli.commands.analytics import _scan_store
        from skillery_cli.core.store_backup import iter_store_skill_dirs

        cfg, target, store, tmp = env
        _install_local(target, store, tmp)
        await _install_from_hub(cfg, target)

        assert (store / ".backups").is_dir()
        assert [d.name for d in iter_store_skill_dirs(store)] == ["demo"]
        assert [i["name"] for i in _scan_store(store)] == ["demo"]


class TestUserDataSurvives:
    """(в) Данные навыка после замены — на месте.

    Общего правила у навыков НЕТ: ``atlas`` держит БД в ``~/.atlas/atlas.db``
    (СНАРУЖИ каталога), а ``telegram-content-cli`` — ``.env`` ВНУТРИ каталога.
    Поэтому проверяются оба случая.
    """

    async def test_data_outside_skill_dir_untouched(self, env) -> None:
        """БД навыка рядом с домом (как ~/.atlas/atlas.db) замена не трогает."""
        cfg, target, store, tmp = env
        home_db = tmp / ".demo" / "demo.db"
        home_db.parent.mkdir()
        home_db.write_bytes(b"portfolio-of-the-owner")
        _install_local(target, store, tmp)

        await _install_from_hub(cfg, target)

        assert home_db.read_bytes() == b"portfolio-of-the-owner"

    async def test_state_inside_skill_dir_carried_over(self, env) -> None:
        """Состояние ВНУТРИ каталога (.env / _local/) переезжает в новую версию."""
        cfg, target, store, tmp = env
        skill_dir = _install_local(target, store, tmp)
        (skill_dir / ".env").write_text("TOKEN=secret", encoding="utf-8")
        (skill_dir / "_local").mkdir()
        (skill_dir / "_local" / "notes.txt").write_text("мои заметки", encoding="utf-8")

        await _install_from_hub(cfg, target)

        assert read_meta(store / "demo")["source"] == "hub"
        assert (store / "demo" / ".env").read_text(encoding="utf-8") == "TOKEN=secret"
        assert (store / "demo" / "_local" / "notes.txt").read_text(
            encoding="utf-8"
        ) == "мои заметки"

    async def test_state_also_kept_in_backup(self, env) -> None:
        """Даже если перенос не понадобился — данные лежат в резерве, не в мусоре."""
        cfg, target, store, tmp = env
        skill_dir = _install_local(target, store, tmp)
        (skill_dir / "custom-state.json").write_text('{"a":1}', encoding="utf-8")

        await _install_from_hub(cfg, target)

        backup = list_backups(store, "demo")[0]
        kept = Path(backup["skill_path"]) / "custom-state.json"
        assert json.loads(kept.read_text(encoding="utf-8")) == {"a": 1}


class TestReconcileDoesNotSkipForeign:
    """Веб-набор (/me/installs) не «пропускает» навык из-за версии чужой установки.

    Локальный ``install --path`` со своей версией (atlas v0.3.9) глушил хаб-
    установку той же/меньшей версии: веб рапортовал успех, а на устройстве
    оставалась и исполнялась локальная версия.
    """

    async def test_foreign_source_is_taken_over(self, env, monkeypatch) -> None:
        cfg, target, store, tmp = env
        _install_local(target, store, tmp)

        class _Client:
            async def list_my_installs(self):
                return [{"slug": "demo", "installed_version": _LOCAL_VERSION}]

            async def close(self):
                return None

        monkeypatch.setattr(m, "HubClient", lambda **kw: _Client(), raising=False)
        called: list[str] = []

        async def _fake_chain(*a, **kw):
            called.append(kw["slug"])
            return []

        monkeypatch.setattr(m, "_install_chain", _fake_chain, raising=False)

        report = await m._reconcile_hub_installs(
            cfg, "token", channel="published", agent_target=target
        )

        assert called == ["demo"], "чужой источник обязан вытесняться, а не пропускаться"
        assert report["skipped"] == []

    async def test_same_version_hub_install_still_skipped(
        self, env, monkeypatch
    ) -> None:
        """Однородная хаб-установка той же версии по-прежнему пропускается."""
        cfg, target, store, tmp = env
        _install_local(target, store, tmp)
        # Делаем установку хабовой той же версии — сравнение версий снова в силе.
        meta = read_meta(store / "demo")
        meta["source"] = "hub"
        (store / "demo" / "_skill_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

        class _Client:
            async def list_my_installs(self):
                return [{"slug": "demo", "installed_version": _LOCAL_VERSION}]

            async def close(self):
                return None

        monkeypatch.setattr(m, "HubClient", lambda **kw: _Client(), raising=False)
        called: list[str] = []

        async def _fake_chain(*a, **kw):
            called.append(kw["slug"])
            return []

        monkeypatch.setattr(m, "_install_chain", _fake_chain, raising=False)

        report = await m._reconcile_hub_installs(
            cfg, "token", channel="published", agent_target=target
        )

        assert called == []
        assert report["skipped"] == ["demo"]
