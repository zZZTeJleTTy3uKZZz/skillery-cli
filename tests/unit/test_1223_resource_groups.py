"""Ресурсные группы команд + back-compat плоских имён (#1223).

Здесь проверяется не «красиво сгруппировали», а РИСК: CLI стоит у
пользователей и зовётся из SKILL.md навыков, из скриптов и из демона.
Поэтому гарантии, которые тесты обязаны держать:

а) каждое прежнее плоское имя ЗАРЕГИСТРИРОВАНО и ведёт в ТУ ЖЕ функцию,
   что новая форма (не копию — иначе формы разойдутся при первой правке);
б) предупреждение об устаревании уходит в **stderr**, не в stdout;
в) под ``--json`` stdout остаётся валидными JSON Lines без посторонних строк;
г) справка показывает ресурсные группы.

⚠️ Известная грабля: тест справки уже падал из-за ширины терминала — rich
переносит строки по ``COLUMNS``. Поэтому здесь НИ ОДНОЙ проверки на перенос:
структура читается из ``registered_groups``/``registered_commands``, а от
рендера требуется только наличие ОДНОСЛОВНЫХ имён групп (их перенести нельзя).
"""
from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from skillery_cli import _grouping, output as output_module
from skillery_cli.config import ClientConfig


def _build_admin_app(monkeypatch: pytest.MonkeyPatch) -> typer.Typer:
    """``build_app`` с максимумом прав — видны все permission-гейтные группы."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["hub.admin"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    return build_app()


def _root_commands(app: typer.Typer) -> dict[str, typer.models.CommandInfo]:
    return {c.name: c for c in app.registered_commands if c.name}


def _group(app: typer.Typer, name: str) -> typer.Typer:
    return next(g.typer_instance for g in app.registered_groups if g.name == name)


def _unwrap(func: object) -> object:
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__  # type: ignore[assignment]
    return func


# ============================================================
# (а) старые имена живы и ведут в ту же функцию
# ============================================================
def test_every_legacy_flat_name_still_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ни одно плоское имя не исчезло — иначе молча ломается чужая автоматизация."""
    app = _build_admin_app(monkeypatch)
    root = _root_commands(app)
    for _group_name, (_help, verbs) in _grouping.RESOURCE_GROUPS.items():
        for old in verbs:
            assert old in root, f"плоское имя `{old}` пропало из CLI"


def test_legacy_alias_calls_the_same_function_as_new_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Алиас делегирует, а не дублирует: под обёрткой — ТОТ ЖЕ объект-функция."""
    app = _build_admin_app(monkeypatch)
    root = _root_commands(app)
    for group_name, (_help, verbs) in _grouping.RESOURCE_GROUPS.items():
        sub = _group(app, group_name)
        sub_cmds = {c.name: c for c in sub.registered_commands}
        for old, verb in verbs.items():
            assert verb in sub_cmds, f"нет `{group_name} {verb}`"
            assert _unwrap(root[old].callback) is _unwrap(sub_cmds[verb].callback), (
                f"`{old}` и `{group_name} {verb}` — РАЗНЫЕ функции"
            )


def test_legacy_flat_names_are_hidden_and_marked_deprecated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Из справки плоские формы уходят, но остаются вызываемыми."""
    app = _build_admin_app(monkeypatch)
    root = _root_commands(app)
    for _group_name, (_help, verbs) in _grouping.RESOURCE_GROUPS.items():
        for old in verbs:
            assert root[old].hidden is True, f"`{old}` не скрыт из справки"
            assert root[old].deprecated is True, f"`{old}` не помечен deprecated"


def test_hot_path_and_standalone_commands_stay_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run` (вшит в шаблон SKILL.md) и разовые действия НЕ переезжают."""
    app = _build_admin_app(monkeypatch)
    root = _root_commands(app)
    for name in _grouping._KEEP_FLAT:
        assert name in root, f"`{name}` должен остаться плоским"
        assert not root[name].hidden, f"`{name}` не должен прятаться из справки"
        assert not root[name].deprecated


def test_legacy_flat_name_is_invokable_end_to_end() -> None:
    """Сквозной прогон: старое имя реально вызывает функцию с теми же опциями."""
    calls: list[str] = []
    app = typer.Typer()

    @app.command(name="widgets")
    def cmd_widgets(name: str = typer.Option("all", "--name")) -> None:
        """Список виджетов."""
        calls.append(name)

    monkey = {"widget": ("Виджеты.", {"widgets": "list"})}
    original = _grouping.RESOURCE_GROUPS
    try:
        _grouping.RESOURCE_GROUPS = monkey  # type: ignore[assignment]
        _grouping.apply_resource_groups(app)
    finally:
        _grouping.RESOURCE_GROUPS = original  # type: ignore[assignment]

    runner = CliRunner()
    assert runner.invoke(app, ["widget", "list", "--name", "a"]).exit_code == 0
    assert runner.invoke(app, ["widgets", "--name", "a"]).exit_code == 0
    # Обе формы отработали и приняли ОДИНАКОВЫЕ опции.
    assert calls == ["a", "a"]


# ============================================================
# (б) предупреждение — в stderr, не в stdout
# ============================================================
def test_warning_goes_to_stderr_in_text_mode(capsys: pytest.CaptureFixture) -> None:
    output_module._mode = "text"
    _grouping._warn_stderr("status", "cli status")
    captured = capsys.readouterr()
    assert captured.out == "", "предупреждение попало в stdout — stdout парсят"
    assert "устарела" in captured.err
    assert "cli status" in captured.err


def test_warning_names_both_old_and_new_form(capsys: pytest.CaptureFixture) -> None:
    output_module._mode = "text"
    _grouping._warn_stderr("members", "member list")
    err = capsys.readouterr().err
    assert "members" in err and "member list" in err


def test_warning_can_be_suppressed_by_env(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_module._mode = "text"
    monkeypatch.setenv(_grouping.SUPPRESS_ENV, "1")
    _grouping._warn_stderr("status", "cli status")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# ============================================================
# (в) --json: stdout — чистые JSON Lines
# ============================================================
def test_json_mode_warning_is_json_line_on_stderr_only(
    capsys: pytest.CaptureFixture,
) -> None:
    output_module._mode = "json"
    try:
        _grouping._warn_stderr("install", "skill install")
        captured = capsys.readouterr()
        assert captured.out == "", "в json-режиме stdout обязан остаться чистым"
        rec = json.loads(captured.err.strip())
        assert rec["event"] == "warn"
        assert rec["deprecated_command"] == "install"
        assert rec["replacement"] == "skill install"
    finally:
        output_module._mode = "text"


def test_json_mode_stdout_stays_parseable_json_lines(
    capsys: pytest.CaptureFixture,
) -> None:
    """Полный контракт: результат в stdout, предупреждение — мимо него."""
    output_module._mode = "json"
    try:
        _grouping._warn_stderr("members", "member list")
        output_module.emit_data({"ok": True, "items": []})
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 1, f"лишние строки в stdout: {lines}"
        assert json.loads(lines[0]) == {"ok": True, "items": []}
    finally:
        output_module._mode = "text"


# ============================================================
# (г) справка: группы видны, к переносам строк не привязываемся
# ============================================================
def test_resource_groups_are_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_admin_app(monkeypatch)
    names = {g.name for g in app.registered_groups}
    for group_name in _grouping.RESOURCE_GROUPS:
        assert group_name in names, f"нет ресурсной группы `{group_name}`"


def test_root_help_shows_group_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Имена групп — ОДНО слово: rich их не переносит, ширина терминала не важна."""
    app = _build_admin_app(monkeypatch)
    result = CliRunner().invoke(app, ["--help"], terminal_width=200)
    assert result.exit_code == 0
    for group_name in _grouping.RESOURCE_GROUPS:
        assert group_name in result.output, f"`{group_name}` нет в корневой справке"


def test_root_help_hides_legacy_flat_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Справка читается по ресурсам: плоские дубли её не засоряют."""
    app = _build_admin_app(monkeypatch)
    output = CliRunner().invoke(app, ["--help"], terminal_width=200).output
    # Берём однословные имена, которые НЕ являются подстрокой чего-то живого
    # (напр. `permissions` сюда не годится — оно внутри `set-permissions`).
    for legacy in ("installed", "contributors", "devices", "accept-invite"):
        assert legacy not in output, f"устаревшее `{legacy}` осталось в справке"


@pytest.mark.parametrize(
    ("group_name", "verb"),
    [
        ("skill", "install"),
        ("auth", "login"),
        ("cli", "doctor"),
        ("device", "list"),
        ("member", "list"),
        ("permission", "list"),
        ("store", "migrate"),
        ("rating", "set"),
        ("role", "list"),
        ("comment", "list"),
    ],
)
def test_group_help_lists_its_verbs(
    monkeypatch: pytest.MonkeyPatch, group_name: str, verb: str
) -> None:
    app = _build_admin_app(monkeypatch)
    # Справку группы снимаем с САМОЙ группы, а не через корень: путь
    # `root <group> --help` выполняет КОРНЕВОЙ callback, а тот навешивает
    # глобальный log-sync handler — процессное состояние, которое протекает
    # в соседние тесты (ровно этот класс флейков ловили с matchMedia).
    result = CliRunner().invoke(_group(app, group_name), ["--help"], terminal_width=200)
    assert result.exit_code == 0
    assert verb in result.output


def test_ticket_list_replaces_plural_tickets_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tickets list` → `ticket list`; множественная группа скрыта, но жива."""
    app = _build_admin_app(monkeypatch)
    ticket = _group(app, "ticket")
    assert "list" in {c.name for c in ticket.registered_commands}
    tickets_info = next(g for g in app.registered_groups if g.name == "tickets")
    assert tickets_info.hidden is True
    legacy = next(
        c for c in tickets_info.typer_instance.registered_commands if c.name == "list"
    )
    canonical = next(c for c in ticket.registered_commands if c.name == "list")
    assert _unwrap(legacy.callback) is _unwrap(canonical.callback)
