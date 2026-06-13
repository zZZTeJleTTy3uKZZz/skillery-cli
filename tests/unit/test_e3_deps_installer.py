"""E3 фаза 2 — core/deps_installer: установка runtime_dependencies навыка.

Манифест навыка (E6) несёт ``runtime_dependencies`` = список ``{kind, spec}``:
- ``pip`` → uv → ``uv pip`` → ``pip`` (graceful degradation, как install.py
  reverse-factory: нет uv → pip; нет pip → warn+инструкция, НЕ падаем);
- ``npm`` → ставим только если есть node/npm, иначе warn;
- ``system`` → НЕ ставим сами, только warn+инструкция.

Идемпотентность — на совести менеджера пакетов (повторный install — no-op для
уже стоящего). Возврат: структура с installed / skipped / failed (никогда не
бросает — degradation вместо падения).

Все вызовы subprocess мокаются — тесты не трогают реальные uv/pip/npm.
"""
from __future__ import annotations

import pytest

from skills_hub_cli.core import deps_installer as di


# --------------------------------------------------------------------------
#  Парс входа: список dict {kind, spec}
# --------------------------------------------------------------------------
def test_install_deps_empty_is_noop() -> None:
    """Пустой список зависимостей → пустой отчёт, ничего не зовём."""
    res = di.install_runtime_dependencies([])
    assert res["installed"] == []
    assert res["skipped"] == []
    assert res["failed"] == []


def test_install_deps_ignores_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кривые элементы (без kind/spec, не-dict) пропускаются, не валят."""
    monkeypatch.setattr(di, "_which", lambda name: None)
    monkeypatch.setattr(di, "_pip_available", lambda: False)
    res = di.install_runtime_dependencies(
        [{"kind": "pip"}, {"spec": "x"}, "garbage", {"kind": "pip", "spec": "httpx"}]
    )
    # Единственная валидная pip-зависимость учтена (как failed/skipped — нет менеджера),
    # мусор не учтён нигде.
    total = len(res["installed"]) + len(res["skipped"]) + len(res["failed"])
    assert total == 1


# --------------------------------------------------------------------------
#  pip-ветка: uv → uv pip → pip fallback
# --------------------------------------------------------------------------
def test_pip_dep_prefers_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если есть uv — ставим через ``uv pip install`` (быстрее)."""
    monkeypatch.setattr(di, "_which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    calls: list[list[str]] = []
    monkeypatch.setattr(di, "_run", lambda cmd: calls.append(cmd) or 0)
    res = di.install_runtime_dependencies([{"kind": "pip", "spec": "httpx>=0.27"}])
    assert res["installed"] == [{"kind": "pip", "spec": "httpx>=0.27"}]
    assert calls, "должен быть вызов менеджера пакетов"
    assert calls[0][0] == "uv"
    assert "pip" in calls[0] and "install" in calls[0]
    assert "httpx>=0.27" in calls[0]


def test_pip_dep_falls_back_to_pip_when_no_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет uv, но есть pip → ставим через ``python -m pip install``."""
    monkeypatch.setattr(di, "_which", lambda name: None)
    monkeypatch.setattr(di, "_pip_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(di, "_run", lambda cmd: calls.append(cmd) or 0)
    res = di.install_runtime_dependencies([{"kind": "pip", "spec": "rich"}])
    assert res["installed"] == [{"kind": "pip", "spec": "rich"}]
    # python -m pip install rich
    assert "pip" in calls[0]
    assert "install" in calls[0]
    assert "rich" in calls[0]


def test_pip_dep_no_manager_warns_not_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ни uv, ни pip → зависимость в skipped с инструкцией, НЕ бросаем."""
    monkeypatch.setattr(di, "_which", lambda name: None)
    monkeypatch.setattr(di, "_pip_available", lambda: False)

    def _boom(cmd: list[str]) -> int:
        raise AssertionError("subprocess не должен вызываться без менеджера")

    monkeypatch.setattr(di, "_run", _boom)
    res = di.install_runtime_dependencies([{"kind": "pip", "spec": "httpx"}])
    assert res["installed"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["spec"] == "httpx"
    assert res["skipped"][0].get("reason")
    assert "instruction" in res["skipped"][0]


def test_pip_dep_install_failure_goes_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Менеджер вернул ненулевой код → зависимость в failed, НЕ исключение."""
    monkeypatch.setattr(di, "_which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(di, "_run", lambda cmd: 1)
    res = di.install_runtime_dependencies([{"kind": "pip", "spec": "broken-pkg"}])
    assert res["failed"] == [{"kind": "pip", "spec": "broken-pkg"}] or (
        len(res["failed"]) == 1 and res["failed"][0]["spec"] == "broken-pkg"
    )
    assert res["installed"] == []


# --------------------------------------------------------------------------
#  npm-ветка
# --------------------------------------------------------------------------
def test_npm_dep_installed_when_npm_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        di, "_which", lambda name: "/usr/bin/npm" if name == "npm" else None
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(di, "_run", lambda cmd: calls.append(cmd) or 0)
    res = di.install_runtime_dependencies([{"kind": "npm", "spec": "@scope/pkg"}])
    assert res["installed"] == [{"kind": "npm", "spec": "@scope/pkg"}]
    assert calls[0][0] == "npm"
    assert "@scope/pkg" in calls[0]


def test_npm_dep_skipped_when_no_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет npm → npm-зависимость в skipped + инструкция, не падаем."""
    monkeypatch.setattr(di, "_which", lambda name: None)
    res = di.install_runtime_dependencies([{"kind": "npm", "spec": "left-pad"}])
    assert res["installed"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["kind"] == "npm"
    assert "instruction" in res["skipped"][0]


# --------------------------------------------------------------------------
#  system-ветка: никогда не ставим сами
# --------------------------------------------------------------------------
def test_system_dep_always_skipped_with_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """system-зависимость — только инструкция, subprocess не зовём вообще."""

    def _boom(cmd: list[str]) -> int:
        raise AssertionError("system-deps не ставятся автоматически")

    monkeypatch.setattr(di, "_run", _boom)
    res = di.install_runtime_dependencies([{"kind": "system", "spec": "ffmpeg"}])
    assert res["installed"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["kind"] == "system"
    assert res["skipped"][0]["spec"] == "ffmpeg"
    assert "instruction" in res["skipped"][0]


# --------------------------------------------------------------------------
#  Смешанный список + порядок не валит на одной плохой
# --------------------------------------------------------------------------
def test_mixed_deps_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    """pip ok + npm нет + system → installed/​skipped разнесены, общий не падает."""
    monkeypatch.setattr(
        di, "_which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(di, "_run", lambda cmd: 0)
    res = di.install_runtime_dependencies(
        [
            {"kind": "pip", "spec": "httpx"},
            {"kind": "npm", "spec": "react"},  # нет npm (which→только uv)
            {"kind": "system", "spec": "ffmpeg"},
        ]
    )
    assert {d["spec"] for d in res["installed"]} == {"httpx"}
    skipped_specs = {d["spec"] for d in res["skipped"]}
    assert skipped_specs == {"react", "ffmpeg"}
