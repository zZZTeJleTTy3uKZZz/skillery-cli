"""Тесты helper'а сравнения версий `_is_newer` и логики `_maybe_auto_update`.

Баг B8: в `_maybe_auto_update` было `if updated_any or True:` (мёртвое условие —
timestamp ставился всегда, флаг бессмыслен) и сравнение версий строкой
`bundle["version"] != current` (downgrade воспринимался как «апдейт»).

`_is_newer(candidate, current)` должен:
- True, только если candidate СТРОГО новее current (semver-подобно);
- толерантно к префиксу `v` (`v1.2.3` == `1.2.3`);
- толерантно к нечисловым хвостам (pre-release / билд-суффиксы);
- при неразборчивой версии — fallback на `!=` (не хуже прежнего поведения).
"""
from __future__ import annotations

import pytest

from skills_hub_cli.__main__ import _is_newer


# ---------------------- _is_newer: основные сравнения ----------------------
@pytest.mark.parametrize(
    "candidate,current",
    [
        ("1.0.1", "1.0.0"),
        ("1.1.0", "1.0.9"),
        ("2.0.0", "1.9.9"),
        ("1.0.0", "0.9.9"),
        ("1.2.10", "1.2.9"),  # числовое, не лексикографическое сравнение
        ("0.0.2", "0.0.1"),
    ],
)
def test_is_newer_true_when_candidate_strictly_newer(
    candidate: str, current: str
) -> None:
    assert _is_newer(candidate, current) is True


@pytest.mark.parametrize(
    "candidate,current",
    [
        ("1.0.0", "1.0.1"),  # downgrade
        ("1.0.9", "1.1.0"),
        ("0.9.9", "1.0.0"),
        ("1.2.9", "1.2.10"),  # downgrade, числовое сравнение
        ("1.0.0", "2.0.0"),
    ],
)
def test_is_newer_false_on_downgrade(candidate: str, current: str) -> None:
    assert _is_newer(candidate, current) is False


@pytest.mark.parametrize(
    "version",
    ["1.0.0", "v1.2.3", "0.0.1", "3.4.5"],
)
def test_is_newer_false_when_equal(version: str) -> None:
    """Равные версии — не апдейт (главный смысл фикса: timestamp двигаем,
    но саму установку НЕ перезапускаем зря)."""
    assert _is_newer(version, version) is False


# ---------------------- _is_newer: префикс `v` ----------------------
@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("v1.0.1", "1.0.0", True),
        ("1.0.1", "v1.0.0", True),
        ("v1.0.0", "v1.0.0", False),
        ("v1.0.0", "1.0.1", False),
        ("V2.0.0", "v1.0.0", True),  # верхний регистр тоже
    ],
)
def test_is_newer_tolerates_v_prefix(
    candidate: str, current: str, expected: bool
) -> None:
    assert _is_newer(candidate, current) is expected


# ---------------------- _is_newer: нечисловые хвосты ----------------------
def test_is_newer_handles_prerelease_tail_numeric_part_wins() -> None:
    # Числовая часть основной версии сравнивается; хвост игнорируется.
    assert _is_newer("1.2.0-rc1", "1.1.0") is True
    assert _is_newer("1.1.0", "1.2.0-rc1") is False


def test_is_newer_extra_segments() -> None:
    # Разное число сегментов: 1.2 vs 1.2.0 → равны (не новее).
    assert _is_newer("1.2", "1.2.0") is False
    assert _is_newer("1.2.1", "1.2") is True
    assert _is_newer("1.2", "1.2.1") is False


# ---------------------- _is_newer: мусор → fallback на != ----------------------
@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("abc", "abc", False),  # равные «мусорные» → не апдейт
        ("abc", "def", True),  # разные нераспарсиваемые → fallback != → True
        ("", "1.0.0", True),  # пустая candidate отличается → != → True
        ("nightly", "nightly", False),
    ],
)
def test_is_newer_garbage_fallback_to_inequality(
    candidate: str, current: str, expected: bool
) -> None:
    assert _is_newer(candidate, current) is expected


# ============== _maybe_auto_update: интеграция (ядро бага B8) ==============
def _setup_auto_update_env(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed_version: str,
    bundle_version: str,
    install_calls: list,
):
    """Готовит окружение для прогона `_maybe_auto_update` с одним установленным
    навыком и застабленным `install_bundle`, отдающим `bundle_version`.
    Любой вызов `SkillInstaller.install` пишется в `install_calls`."""
    import skills_hub_cli.__main__ as main_mod
    from skills_hub_cli.config import ClientConfig
    from skills_hub_cli.core.agents import ClaudeCodeTarget

    # Global scope с одним «установленным» навыком demo@installed_version.
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    base = target.base_dir()
    skill_dir = base / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("hi", encoding="utf-8")
    (skill_dir / "_skill_meta.json").write_text(
        f'{{"slug": "demo", "version": "{installed_version}"}}',
        encoding="utf-8",
    )

    cfg = ClientConfig(
        store_dir=str(tmp_path / "store"),
        base_url="http://localhost:8000",
    )
    cfg.auto_update = True
    cfg.auto_update_cooldown_min = 60
    cfg.last_auto_update_at = None
    cfg.user_email = "x@y.io"
    # is_logged_in() — у тестового cfg делаем True безусловно.
    monkeypatch.setattr(ClientConfig, "is_logged_in", lambda self: True)

    saved: dict[str, bool] = {"saved": False}
    monkeypatch.setattr(
        ClientConfig, "save", lambda self: saved.__setitem__("saved", True)
    )
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(
        main_mod, "load_tokens", lambda email: ("access-tok", "refresh-tok")
    )
    monkeypatch.setattr(
        main_mod, "_make_refresh_callback", lambda c: None
    )

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            return {
                "version": bundle_version,
                "commit_sha": "deadbeef",
                "repo_url": None,
                "manifest": {"version": bundle_version, "files": []},
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)

    def _fake_install(self, **kw):
        install_calls.append(kw)

    monkeypatch.setattr(main_mod.SkillInstaller, "install", _fake_install)
    return main_mod, cfg, saved


def test_maybe_auto_update_skips_install_when_equal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8: установленная == опубликованной → install НЕ вызывается, но
    cooldown-таймстамп всё равно сохраняется."""
    install_calls: list = []
    main_mod, cfg, saved = _setup_auto_update_env(
        tmp_path,
        monkeypatch,
        installed_version="1.0.0",
        bundle_version="1.0.0",
        install_calls=install_calls,
    )
    main_mod._maybe_auto_update(cfg)
    assert install_calls == []  # ничего не переустанавливали зря
    assert saved["saved"] is True  # таймстамп всё равно подвинут
    assert cfg.last_auto_update_at is not None


def test_maybe_auto_update_skips_install_on_downgrade(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8: опубликованная СТАРШЕ установленной → НЕ откатываем вниз."""
    install_calls: list = []
    main_mod, cfg, _ = _setup_auto_update_env(
        tmp_path,
        monkeypatch,
        installed_version="2.0.0",
        bundle_version="1.0.0",
        install_calls=install_calls,
    )
    main_mod._maybe_auto_update(cfg)
    assert install_calls == []


def test_maybe_auto_update_installs_when_newer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8: опубликованная строго новее → install вызывается ровно один раз
    с новой версией."""
    install_calls: list = []
    main_mod, cfg, _ = _setup_auto_update_env(
        tmp_path,
        monkeypatch,
        installed_version="1.0.0",
        bundle_version="1.1.0",
        install_calls=install_calls,
    )
    main_mod._maybe_auto_update(cfg)
    assert len(install_calls) == 1
    assert install_calls[0]["slug"] == "demo"
    assert install_calls[0]["version"] == "1.1.0"
