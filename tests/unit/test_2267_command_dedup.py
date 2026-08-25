"""#2267: одно действие = одна команда и одна реализация.

Проверяется не «красиво переименовали», а РИСК. Их два, и они разнонаправлены:

а) **Двоение.** Пока одно действие достижимо двумя ВИДИМЫМИ путями
   (``admin invite`` и ``member invite``, ``admin company-create`` и
   ``company create``), пользователь не знает, какой из них настоящий, а
   правка одного молча расходится со вторым. Поэтому тест ищет двоение
   двумя независимыми способами: по тождеству callback'а (одна функция под
   двумя именами) и по вызываемой ручке транспорта (две РАЗНЫЕ реализации
   одного действия — их тождество callback'а не ловит).

б) **Обрыв совместимости.** CLI стоит у пользователей и зовётся из скриптов
   и SKILL.md навыков. Снятое имя обязано продолжать работать — скрытым
   deprecated-алиасом в ТУ ЖЕ функцию и с предупреждением в stderr.

Плюс — мёртвая вторая точка регистрации команд (``comment.register``), из-за
которой в репозитории было два источника правды об именах команд.
"""
from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import pytest
import typer

from skillery_cli.config import ClientConfig

#: Ручка транспорта → каноничный ВИДИМЫЙ путь команды, который её дёргает.
#: Ровно те четыре действия, вокруг которых было двоение (#2267).
_ACTION_CANON: dict[str, str] = {
    "issue_invite": "member invite",
    "create_company": "company create",
    "yank_skill_version": "skill yank",
    "sync_skill": "skill sync-versions",
}


def _build_max_rights_app(monkeypatch: pytest.MonkeyPatch) -> typer.Typer:
    """``build_app`` с hub.admin — видны ВСЕ permission-гейтные команды."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["hub.admin"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    return build_app()


def _unwrap(func: Any) -> Any:
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


def _visible_paths(app: typer.Typer) -> list[tuple[str, Callable[..., Any]]]:
    """Все ВИДИМЫЕ пути CLI: ``(путь, развёрнутый callback)``.

    Скрытое (``hidden``) и депрекейтнутое (``deprecated``) не считается —
    это и есть механизм совместимости: имя работает, но каноном не является.
    Скрытая группа скрывает и всё своё содержимое.
    """

    def walk(
        sub: typer.Typer, prefix: list[str]
    ) -> list[tuple[str, Callable[..., Any]]]:
        found: list[tuple[str, Callable[..., Any]]] = []
        for cmd in sub.registered_commands:
            if cmd.hidden is True or cmd.deprecated is True or cmd.callback is None:
                continue
            found.append((" ".join([*prefix, cmd.name or ""]), _unwrap(cmd.callback)))
        for group in sub.registered_groups:
            if group.hidden is True or group.deprecated is True:
                continue
            if group.typer_instance is None:
                continue
            found += walk(group.typer_instance, [*prefix, group.name or ""])
        return found

    return walk(app, [])


def _all_paths(app: typer.Typer) -> dict[str, Callable[..., Any]]:
    """Все пути, включая скрытые (для проверок обратной совместимости)."""

    def walk(sub: typer.Typer, prefix: list[str]) -> dict[str, Callable[..., Any]]:
        found: dict[str, Callable[..., Any]] = {}
        for cmd in sub.registered_commands:
            if cmd.callback is None:
                continue
            found[" ".join([*prefix, cmd.name or ""])] = cmd.callback
        for group in sub.registered_groups:
            if group.typer_instance is None:
                continue
            found.update(walk(group.typer_instance, [*prefix, group.name or ""]))
        return found

    return walk(app, [])


# ============================================================
# (а) двоение — по тождеству функции
# ============================================================
def test_no_function_is_reachable_by_two_visible_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Одна функция под двумя ВИДИМЫМИ именами = пользователь не знает канон."""
    app = _build_max_rights_app(monkeypatch)
    by_func: dict[Any, list[str]] = defaultdict(list)
    for path, func in _visible_paths(app):
        by_func[func].append(path)
    dupes = {
        f"{f.__module__}.{f.__name__}": sorted(paths)
        for f, paths in by_func.items()
        if len(paths) > 1
    }
    assert not dupes, f"одно действие достижимо несколькими видимыми путями: {dupes}"


# ============================================================
# (а) двоение — по вызываемой ручке транспорта
# ============================================================
@pytest.mark.parametrize(("transport_call", "canon_path"), _ACTION_CANON.items())
def test_action_has_exactly_one_visible_command(
    monkeypatch: pytest.MonkeyPatch, transport_call: str, canon_path: str
) -> None:
    """Две РАЗНЫЕ реализации одного действия — тождество функций их не ловит.

    Поэтому смотрим на то, что команда РЕАЛЬНО делает: какую ручку транспорта
    зовёт. ``admin invite`` и ``member invite`` были именно таким случаем —
    разные функции, один ``client.issue_invite`` и один ``POST /invites``.
    """
    app = _build_max_rights_app(monkeypatch)
    hits = []
    for path, func in _visible_paths(app):
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):  # pragma: no cover — исходник есть у всех
            continue
        if f".{transport_call}(" in source:
            hits.append(path)
    assert sorted(hits) == [canon_path], (
        f"`{transport_call}` должен быть достижим ровно одной видимой "
        f"командой `{canon_path}`, а достижим: {sorted(hits)}"
    )


# ============================================================
# группа admin расформирована
# ============================================================
def test_admin_group_is_hidden_and_empty_of_canon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`admin` больше не канон: группа скрыта, все её глаголы — алиасы."""
    app = _build_max_rights_app(monkeypatch)
    visible = {path for path, _ in _visible_paths(app)}
    leaked = sorted(p for p in visible if p == "admin" or p.startswith("admin "))
    assert not leaked, f"группа admin всё ещё видима: {leaked}"
    admin_group = next((g for g in app.registered_groups if g.name == "admin"), None)
    assert admin_group is not None, "группа admin должна остаться как back-compat"
    assert admin_group.hidden is True


def test_new_canon_paths_are_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Действия переехали в группы своих сущностей и там ВИДНЫ."""
    app = _build_max_rights_app(monkeypatch)
    visible = {path for path, _ in _visible_paths(app)}
    for path in (
        "skill yank",
        "skill sync-versions",
        "company create",
        "member invite",
    ):
        assert path in visible, f"каноничный путь `{path}` не виден в CLI"


# ============================================================
# (б) обратная совместимость снятых имён
# ============================================================
@pytest.mark.parametrize(
    ("old_path", "new_path"),
    [
        ("admin sync-skill", "skill sync-versions"),
        ("admin yank", "skill yank"),
        ("admin company-create", "company create"),
        ("admin invite", "member invite"),
    ],
)
def test_old_admin_name_still_calls_the_same_function(
    monkeypatch: pytest.MonkeyPatch, old_path: str, new_path: str
) -> None:
    """Алиас делегирует, а не дублирует: под обёрткой — ТОТ ЖЕ объект-функция."""
    app = _build_max_rights_app(monkeypatch)
    paths = _all_paths(app)
    assert old_path in paths, f"старое имя `{old_path}` пропало — сломает скрипты"
    canon = dict(_visible_paths(app))
    assert _unwrap(paths[old_path]) is canon[new_path]


def test_old_admin_name_warns_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Молчаливый алиас консервирует скрипты на старом имени — он обязан
    называть новое, и строго в stderr (stdout — машинный канал)."""
    from skillery_cli import _grouping

    monkeypatch.delenv(_grouping.SUPPRESS_ENV, raising=False)
    app = _build_max_rights_app(monkeypatch)
    alias = _all_paths(app)["admin yank"]

    called: dict[str, Any] = {}

    def _fake_run(coro: Any) -> None:
        called["ran"] = True
        coro.close()

    monkeypatch.setattr("skillery_cli.__main__._run", _fake_run)
    monkeypatch.setattr("skillery_cli.__main__._get_access_token", lambda: "tok")
    monkeypatch.setattr("skillery_cli.__main__._make_refresh_callback", lambda c: None)

    alias(slug="demo", version="1.0.0", unyank=False)

    captured = capsys.readouterr()
    assert "admin yank" in captured.err
    assert "skill yank" in captured.err
    assert "admin yank" not in captured.out
    assert called.get("ran") is True


# ============================================================
# права invite: объединение, а не молчаливый выбор одного
# ============================================================
@pytest.mark.parametrize("permission", ["user.invite", "invite.manage"])
def test_member_invite_visible_for_either_invite_permission(
    monkeypatch: pytest.MonkeyPatch, permission: str
) -> None:
    """Слияние двух копий не должно отнять команду ни у одной из прежних
    аудиторий: гейт — ANY-of ``user.invite`` | ``invite.manage``."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=[permission],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    visible = {path for path, _ in _visible_paths(app)}
    assert (
        "member invite" in visible
    ), f"с правом {permission} команда выдачи инвайта пропала из CLI"


# ============================================================
# мёртвая вторая точка регистрации
# ============================================================
def test_comment_module_has_no_dead_register() -> None:
    """``comment.register`` объявлял команды, но не вызывался ниоткуда —
    второй источник правды об именах команд. Его быть не должно."""
    from skillery_cli.commands import comment as comment_mod

    assert not hasattr(comment_mod, "register"), (
        "comment.register вернулся: либо вызывайте его из build_app, либо "
        "не держите вторую точку регистрации имён команд"
    )
