"""#1405 — хабовая версия становится главной И В ЗОНЕ АГЕНТА, а не только в сторе.

СИМПТОМ С ЖИВОЙ МАШИНЫ ВЛАДЕЛЬЦА (снят с диска, а не придуман). В сторе лежит
хабовая версия навыка (``~/.skillery/store/vk`` → ``source=hub`` v0.2.6), а
``~/.claude/skills/vk`` — ПОСТОРОННИЙ каталог с тем же именем: исполняется он.
Соседний случай — ``~/.claude/skills/akzs-io-cli`` — junction прямо в рабочую
папку автора (``_storage/akzs-io-cli`` с ``.git``).

Стор — только хранилище; главным навык делает ИМЯ В ЗОНЕ АГЕНТА. Пока оно
занято чужим, «установка из Хаба прошла» — неправда, чем бы ни закончилась
запись в стор.

ДАННЫЕ. Освобождение имени недеструктивно и РАЗНОЕ по природе занятого пути:

* обычный каталог — это и есть навык (внутри может быть ``.env``): уезжает в
  резерв ЦЕЛИКОМ, состояние переносится в свежую установку;
* ссылка на рабочую папку автора — снимается ТОЛЬКО ССЫЛКА. Её цель (репозиторий
  пользователя) не перемещается и не удаляется: увезти её в служебную зону стора
  значило бы забрать у человека рабочий каталог.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from skillery_cli import __main__ as m
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import read_meta
from skillery_cli.core.store_backup import (
    KIND_SCOPE_DIR,
    KIND_SCOPE_LINK,
    list_backups,
    restore_backup,
)

_HUB_VERSION = "2.0.0"


def _snapshot() -> bytes:
    """tar.gz репозитория с навыком в КОРНЕ (как раздаёт бэкенд)."""
    buf = io.BytesIO()
    body = f"---\nname: demo\nversion: {_HUB_VERSION}\n---\n\n# demo (ХАБ)\n"
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
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    store.mkdir()

    cfg = ClientConfig(store_dir=str(store))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(m, "get_target", lambda name: target, raising=False)
    monkeypatch.setattr(m, "track_skill_event", lambda et, **kw: None, raising=False)
    monkeypatch.setattr(m, "_apply_tooling", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(m, "_emit_onboarding", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(
        m, "HubClient", lambda **kw: _FakeHubClient(_snapshot()), raising=False
    )
    return cfg, target, store, tmp_path


def _foreign_scope_dir(target, *, with_env: bool = True) -> Path:
    """Посторонний каталог навыка в зоне агента (без нашей меты) — как ``vk``."""
    link = Path(target.slug_dir("demo", project=None))
    link.mkdir(parents=True)
    (link / "SKILL.md").write_text(
        "---\nname: demo\nversion: 0.0.9\n---\n\n# demo (ЛОКАЛЬНЫЙ)\n",
        encoding="utf-8",
    )
    if with_env:
        (link / ".env").write_text("TOKEN=секрет\n", encoding="utf-8")
    return link


def _foreign_scope_link(target, tmp_path: Path) -> tuple[Path, Path]:
    """Ссылка зоны агента в РАБОЧУЮ ПАПКУ автора — как ``akzs-io-cli``."""
    from skillkit import linker

    work = tmp_path / "_storage" / "demo"
    (work / ".git").mkdir(parents=True)
    (work / "SKILL.md").write_text(
        "---\nname: demo\nversion: 0.0.9\n---\n\n# demo (РАБОЧАЯ ПАПКА)\n",
        encoding="utf-8",
    )
    link = Path(target.slug_dir("demo", project=None))
    link.parent.mkdir(parents=True, exist_ok=True)
    linker.create_link(link, work)
    return link, work


async def _install_from_hub(cfg, target) -> list[dict]:
    return await m._install_chain(
        cfg, "token", slug="demo", channel="published", scope="global",
        project_path=None, force=False, agent_target=target, headless=True,
    )


def _active_skill_md(link: Path) -> str:
    return (link / "SKILL.md").read_text(encoding="utf-8")


class TestForeignDirInAgentZone:
    """Имя в зоне агента занято ПОСТОРОННИМ каталогом (случай ``vk``)."""

    async def test_hub_version_becomes_active(self, env) -> None:
        """Главной становится хабовая: по этому имени исполняется она."""
        cfg, target, store, tmp = env
        link = _foreign_scope_dir(target)

        await _install_from_hub(cfg, target)

        assert read_meta(store / "demo")["source"] == "hub"
        assert "ХАБ" in _active_skill_md(link), (
            "в зоне агента обязана оказаться хабовая версия — иначе при вызове "
            "навыка исполняется прежняя локальная"
        )
        assert (link / "hub_only.txt").is_file()

    async def test_previous_dir_is_backed_up_not_deleted(self, env) -> None:
        """Прежний каталог не затёрт молча — он целиком в резерве."""
        cfg, target, store, tmp = env
        _foreign_scope_dir(target)

        chain = await _install_from_hub(cfg, target)

        record = chain[0]["replaced_scope"]
        assert record["kind"] == KIND_SCOPE_DIR
        saved = Path(record["skill_path"]) / "SKILL.md"
        assert "ЛОКАЛЬНЫЙ" in saved.read_text(encoding="utf-8")
        assert any(b["id"] == record["id"] for b in list_backups(store, "demo"))

    async def test_user_state_carried_over(self, env) -> None:
        """``.env`` прежнего каталога переезжает в свежую установку."""
        cfg, target, store, tmp = env
        _foreign_scope_dir(target)

        await _install_from_hub(cfg, target)

        carried = store / "demo" / ".env"
        assert carried.is_file(), "секреты навыка обязаны пережить замену"
        assert "секрет" in carried.read_text(encoding="utf-8")

    async def test_rollback_returns_previous(self, env) -> None:
        """Откат возвращает прежний каталог на место (и сам обратим)."""
        cfg, target, store, tmp = env
        link = _foreign_scope_dir(target)

        chain = await _install_from_hub(cfg, target)
        restore_backup(store, "demo", chain[0]["replaced_scope"]["id"])

        assert "ЛОКАЛЬНЫЙ" in _active_skill_md(link)
        # Вытесненная хаб-ссылка не удалена бесследно — есть чем вернуться.
        assert any(
            b["kind"] in (KIND_SCOPE_DIR, KIND_SCOPE_LINK)
            for b in list_backups(store, "demo")
        )

    async def test_failed_install_returns_previous(self, env, monkeypatch) -> None:
        """Провал установки не имеет права оставить зону агента пустой."""
        cfg, target, store, tmp = env
        link = _foreign_scope_dir(target)

        async def _boom(*a, **kw):
            raise RuntimeError("clone упал")

        monkeypatch.setattr(m, "_materialize_from_bundle", _boom, raising=False)

        with pytest.raises(RuntimeError):
            await _install_from_hub(cfg, target)

        assert "ЛОКАЛЬНЫЙ" in _active_skill_md(link)


class TestForeignLinkInAgentZone:
    """Имя занято ССЫЛКОЙ в рабочую папку автора (случай ``akzs-io-cli``)."""

    async def test_hub_version_becomes_active(self, env) -> None:
        cfg, target, store, tmp = env
        link, work = _foreign_scope_link(target, tmp)

        await _install_from_hub(cfg, target)

        assert "ХАБ" in _active_skill_md(link)

    async def test_work_dir_of_author_is_untouched(self, env) -> None:
        """⚠️ Рабочий репозиторий автора не переезжает и не удаляется."""
        cfg, target, store, tmp = env
        link, work = _foreign_scope_link(target, tmp)

        chain = await _install_from_hub(cfg, target)

        assert (work / ".git").is_dir(), "чужой рабочий каталог трогать нельзя"
        assert "РАБОЧАЯ ПАПКА" in (work / "SKILL.md").read_text(encoding="utf-8")
        record = chain[0]["replaced_scope"]
        assert record["kind"] == KIND_SCOPE_LINK
        assert Path(record["link_target"]) == work
        # В служебную зону стора рабочая папка НЕ копировалась.
        assert not (Path(record["path"]) / "skill").exists()

    async def test_no_state_scraped_from_cwd(self, env, monkeypatch) -> None:
        """⚠️ У резерва-ССЫЛКИ перенос состояния не шарит по чужим каталогам.

        В записи такого резерва нет ``skill_path`` (своего содержимого в слоте
        нет). ``Path("")`` в pathlib равен ``Path(".")`` — без явной проверки
        перенос состояния принимал ТЕКУЩИЙ каталог за источник и утаскивал в
        свежую установку посторонний ``.env`` из CWD.
        """
        cfg, target, store, tmp = env
        cwd = tmp / "cwd"
        (cwd / "_local").mkdir(parents=True)
        (cwd / ".env").write_text("CHUZHOY=секрет\n", encoding="utf-8")
        monkeypatch.chdir(cwd)
        _foreign_scope_link(target, tmp)

        await _install_from_hub(cfg, target)

        assert not (store / "demo" / ".env").exists(), (
            "состояние из постороннего каталога не имеет права попасть в навык"
        )
        assert not (store / "demo" / "_local").exists()

    async def test_rollback_recreates_link(self, env) -> None:
        cfg, target, store, tmp = env
        link, work = _foreign_scope_link(target, tmp)

        chain = await _install_from_hub(cfg, target)
        restore_backup(store, "demo", chain[0]["replaced_scope"]["id"])

        from skillkit import linker

        assert linker.is_link(link)
        assert linker.link_target(link) == work
        assert "РАБОЧАЯ ПАПКА" in _active_skill_md(link)


class TestConflictIsNotMaskedAsCloneFailure:
    """Конфликт имени НЕ маскируется «битым снапшотом» → git clone.

    Раньше широкий ``except`` вокруг установки из снапшота уводил ownership-гейт
    на откат к ``git clone``: снапшот уже лёг в стор, а человек получал «нет
    доступа к приватному репозиторию» — причина и лечение назывались чужие.
    """

    async def test_scope_conflict_propagates(self, env, monkeypatch) -> None:
        from skillkit.errors import ScopeConflict

        cfg, target, store, tmp = env
        clones: list = []

        def _boom(*a, **kw):
            raise ScopeConflict("каталог занят и не управляется")

        def _clone(*a, **kw):
            clones.append(kw)
            raise AssertionError("до git clone дойти не должно")

        monkeypatch.setattr(
            m.SkillInstaller, "install_from_snapshot", _boom, raising=False
        )
        monkeypatch.setattr(m.SkillInstaller, "install", _clone, raising=False)

        with pytest.raises(ScopeConflict):
            await _install_from_hub(cfg, target)
        assert clones == []


class TestOwnInstallUntouched:
    """Наша же ссылка в стор — не «чужое»: резерв не плодится на ровном месте."""

    async def test_second_hub_install_creates_no_scope_backup(self, env) -> None:
        cfg, target, store, tmp = env

        await _install_from_hub(cfg, target)
        chain = await _install_from_hub(cfg, target)

        assert "replaced_scope" not in chain[0]
        assert [b for b in list_backups(store, "demo") if b.get("kind")] == []
