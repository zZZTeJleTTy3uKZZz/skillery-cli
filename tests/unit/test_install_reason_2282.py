"""#2282 — ПРИЧИНА установки: плагин расширяет навык, а не становится навыком.

Живой разбор владельца. На чистой машине ``skillery skill install
hello-consumer --scope global`` приносил ОБА навыка, и ``hello-base`` ложился в
зону агента ``~/.claude/skills/hello-base`` как самостоятельный: агент видел его
списком и мог позвать напрямую. В ``_skill_meta.json`` обеих записей не было ни
одного поля о причине установки — система не различала «приехал как
зависимость» и «поставлен явно» вовсе.

Здесь закреплены все пять пунктов ЦКП:

1. причина установки хранится в метаданных (``install_reason``/``required_by``);
2. навык, приехавший ТОЛЬКО как зависимость, в зону агента не попадает
   (но лежит в сторе — потребителю доступен);
3. явная установка того же навыка повышает его до полноценного, повторный
   проход установки уже установленное не ломает;
4. снятие потребителя убирает зависимость, если её больше никто не требует И
   она не была установлена явно; явная — остаётся;
5. ``skillery skill installed`` показывает причину установки.

Тесты гоняют НАСТОЯЩИЙ ``_install_chain`` (реальный ``SkillInstaller``, stub-
источник — как в ``test_store_install``), подменяя только сеть: так проверяется
именно раскладка на диске, ради которой правка и делалась.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core import install_reason, linker
from skillery_cli.core.agents import ClaudeCodeTarget

_CONSUMER = "hello-consumer"
_BASE = "hello-base"


def _bundles() -> dict[str, dict]:
    return {
        _CONSUMER: {
            "skill_slug": _CONSUMER,
            "skill_id": None,
            "version": "1.0.0",
            "commit_sha": "c0ffee",
            "repo_url": None,
            "manifest": {"version": "1.0.0", "files": []},
            # Порядок как у бэкенда: зависимость раньше потребителя.
            "dependencies_chain": [
                [_BASE, "1.0.0", None],
                [_CONSUMER, "1.0.0", None],
            ],
        },
        _BASE: {
            "skill_slug": _BASE,
            "skill_id": None,
            "version": "1.0.0",
            "commit_sha": "cafe01",
            "repo_url": None,
            "manifest": {"version": "1.0.0", "files": []},
            "dependencies_chain": [[_BASE, "1.0.0", None]],
        },
    }


@pytest.fixture
def stand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Стенд «хаб + чистая машина»: реальный стор и реальная зона агента."""
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig

    monkeypatch.setenv("SKILLERY_CONFIG_DIR", str(tmp_path / "cfg"))
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    cfg = ClientConfig(store_dir=str(store), base_url="http://x")
    cfg.user_email = "x@y.io"

    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod, "get_target", lambda name=None: target)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_apply_tooling", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_revert_tooling", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_emit_onboarding", lambda *a, **k: None)

    bundles = _bundles()

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            return bundles[slug]

        async def download_snapshot(self, ref: str, version: str):
            raise RuntimeError("нет снапшота — ставим stub-источником")

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)

    class _Stand:
        main = main_mod
        cfg_ = cfg
        target_ = target
        store_ = store

        def install(self, slug: str) -> list[dict]:
            import asyncio

            return asyncio.run(
                main_mod._install_chain(
                    cfg, "tok", slug=slug, channel="published", scope="global",
                    project_path=None, force=False, agent_target=target,
                )
            )

        def agent_sees(self, slug: str) -> bool:
            d = target.slug_dir(slug)
            return d.exists() or linker.is_link(d)

        def in_store(self, slug: str) -> bool:
            return (store / slug / "SKILL.md").exists()

        def reason(self, slug: str) -> tuple[str, list[str]]:
            return install_reason.read_reason(store / slug)

    return _Stand()


# --- (1) причина установки хранится в метаданных ----------------------------


def test_reason_is_written_into_install_meta(stand) -> None:  # noqa: ANN001
    """У потребителя — explicit, у зависимости — dependency + кем требуется."""
    stand.install(_CONSUMER)
    assert stand.reason(_CONSUMER) == (install_reason.EXPLICIT, [])
    assert stand.reason(_BASE) == (install_reason.DEPENDENCY, [_CONSUMER])


def test_chain_entry_reports_reason(stand) -> None:  # noqa: ANN001
    """Причина видна и в машинном ответе установки (веб/скрипты/агент)."""
    chain = stand.install(_CONSUMER)
    by_slug = {row["slug"]: row for row in chain}
    assert by_slug[_CONSUMER]["install_reason"] == install_reason.EXPLICIT
    assert by_slug[_CONSUMER]["agent_visible"] is True
    assert by_slug[_BASE]["install_reason"] == install_reason.DEPENDENCY
    assert by_slug[_BASE]["agent_visible"] is False


# --- (2) зависимость НЕ появляется в зоне агента ----------------------------


def test_dependency_stays_out_of_agent_zone(stand) -> None:  # noqa: ANN001
    """Ровно то, что владелец увидел живьём: hello-base не должен быть навыком."""
    stand.install(_CONSUMER)
    assert stand.agent_sees(_CONSUMER), "потребитель обязан быть виден агенту"
    assert not stand.agent_sees(_BASE), (
        "зависимость попала в зону агента — агент увидит плагин отдельным навыком"
    )
    # ...но потребителю она доступна: контент лежит в сторе.
    assert stand.in_store(_BASE)


# --- (3) явная установка повышает до полноценного, повтор не ломает ---------


def test_explicit_install_promotes_dependency(stand) -> None:  # noqa: ANN001
    stand.install(_CONSUMER)
    assert not stand.agent_sees(_BASE)
    stand.install(_BASE)  # пользователь ставит его сам
    assert stand.agent_sees(_BASE), "явная установка не сделала навык полноценным"
    reason, required_by = stand.reason(_BASE)
    assert reason == install_reason.EXPLICIT
    # Кем требуется — не забыто: потребитель по-прежнему на него опирается.
    assert required_by == [_CONSUMER]


def test_second_pass_does_not_break_promoted_skill(stand) -> None:  # noqa: ANN001
    """Идемпотентность: повторная установка потребителя не разжалует базу."""
    stand.install(_CONSUMER)
    stand.install(_BASE)
    stand.install(_CONSUMER)
    assert stand.agent_sees(_BASE), "повторный проход снял уже явный навык"
    assert stand.reason(_BASE)[0] == install_reason.EXPLICIT
    assert stand.agent_sees(_CONSUMER)
    assert stand.in_store(_BASE) and stand.in_store(_CONSUMER)


def test_second_pass_keeps_dependency_hidden(stand) -> None:  # noqa: ANN001
    """И наоборот: повтор без явной установки базы не «протаскивает» её агенту."""
    stand.install(_CONSUMER)
    stand.install(_CONSUMER)
    assert not stand.agent_sees(_BASE)
    assert stand.reason(_BASE) == (install_reason.DEPENDENCY, [_CONSUMER])


# --- (4) снятие потребителя убирает осиротевшую зависимость -----------------


def _remove(stand, slug: str) -> None:  # noqa: ANN001
    captured: dict = {}
    stand.main.emit_data = lambda data, **kw: captured.update(data)  # type: ignore[assignment]
    stand.main.cmd_remove(
        slug=slug, scope="global", project=None, keep_local=False,
        purge=True, agent=None,
    )
    return captured


def test_orphan_dependency_leaves_with_consumer(stand, monkeypatch) -> None:  # noqa: ANN001
    stand.install(_CONSUMER)
    monkeypatch.setattr(stand.main, "emit_data", lambda data, **kw: None)
    captured = _remove(stand, _CONSUMER)
    assert not stand.in_store(_BASE), (
        "зависимость осталась на диске, хотя её больше никто не требует"
    )
    assert captured.get("removed_dependencies") == [_BASE]


def test_explicitly_installed_dependency_survives(stand, monkeypatch) -> None:  # noqa: ANN001
    """Явно поставленный навык не сносится вместе с потребителем (apt-инвариант)."""
    stand.install(_CONSUMER)
    stand.install(_BASE)  # повысили до полноценного
    monkeypatch.setattr(stand.main, "emit_data", lambda data, **kw: None)
    captured = _remove(stand, _CONSUMER)
    assert stand.in_store(_BASE), "снесли навык, который пользователь ставил сам"
    assert stand.agent_sees(_BASE)
    assert captured.get("removed_dependencies") == []


def test_dependency_shared_by_two_consumers_stays(stand, monkeypatch) -> None:  # noqa: ANN001
    """Пока хоть один потребитель требует зависимость — она остаётся."""
    stand.install(_CONSUMER)
    install_reason.stamp(
        stand.store_ / _BASE, reason=install_reason.DEPENDENCY,
        required_by="other-consumer",
    )
    monkeypatch.setattr(stand.main, "emit_data", lambda data, **kw: None)
    _remove(stand, _CONSUMER)
    assert stand.in_store(_BASE)
    assert stand.reason(_BASE) == (install_reason.DEPENDENCY, ["other-consumer"])


# --- (5) выдача installed показывает причину --------------------------------


def test_installed_shows_install_reason(stand) -> None:  # noqa: ANN001
    from skillery_cli.commands.installed import collect_installed, render_installed

    stand.install(_CONSUMER)
    payload = collect_installed(stand.cfg_, scope="global")
    rows = {r["name"]: r for r in payload["installed"]}
    assert rows[_BASE]["install_reason"] == install_reason.DEPENDENCY
    assert rows[_BASE]["required_by"] == [_CONSUMER]
    assert rows[_CONSUMER]["install_reason"] == install_reason.EXPLICIT

    printed: list[str] = []

    class _Console:
        def print(self, text: str = "") -> None:
            printed.append(text)

    render_installed(_Console(), payload)
    text = "\n".join(printed)
    assert "зависимость" in text
    assert _CONSUMER in text


# --- совместимость с уже установленным --------------------------------------


def test_meta_without_reason_reads_as_explicit(tmp_path: Path) -> None:
    """Старая запись без поля — явная установка (иначе однажды снесём её как сироту)."""
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "_skill_meta.json").write_text('{"slug": "legacy", "version": "1.0.0"}',
                                        encoding="utf-8")
    assert install_reason.read_reason(d) == (install_reason.EXPLICIT, [])
    assert install_reason.is_agent_visible(d) is True
