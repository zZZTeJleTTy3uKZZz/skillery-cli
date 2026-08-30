"""Ресурсные группы команд (#1223) + back-compat алиасы плоских имён.

Канон — как в клиенте Atlas: ``skillery <ресурс> <глагол>`` (``skill install``,
``member list``), ресурс в ЕДИНСТВЕННОМ числе. Исторически CLI рос плоско
(``install``, ``members``, ``permissions``), и частично уже был сгруппирован
вручную (``comment``/``rating``/``invite``, canon #890) — эта единая точка
доводит группировку до конца и делает её декларативной.

ГЛАВНОЕ ОГРАНИЧЕНИЕ — ОБРАТНАЯ СОВМЕСТИМОСТЬ. CLI стоит у пользователей и
зовётся из SKILL.md навыков, из скриптов и из демона: сломать имя команды —
значит молча сломать чужую автоматизацию. Поэтому:

* КАЖДОЕ прежнее плоское имя остаётся зарегистрированным и рабочим;
* оно ведёт в ТУ ЖЕ функцию, что и новая форма (не копия — иначе формы
  разойдутся при первой же правке);
* при вызове старого имени в **stderr** уходит предупреждение с новым именем.
  Именно в stderr: stdout — машинный канал, его парсят;
* в ``--json`` предупреждение — отдельная JSON-строка в stderr, а stdout
  остаётся чистыми JSON Lines результата.

Плоские формы помечаются ``hidden=True`` + ``deprecated=True``: из ``--help``
уходят (справка читается по группам), но остаются вызываемыми.

Что СОЗНАТЕЛЬНО не группируется (см. ``_KEEP_FLAT``) — команды без ресурса-
собрата и горячий путь агентов.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import sys
from typing import Any, Callable

import typer

#: Env-выключатель предупреждений (для скриптов, которые сознательно остались
#: на старых именах и не хотят шума в stderr).
SUPPRESS_ENV = "SKILLERY_NO_DEPRECATION_WARNINGS"

#: Плоское имя → ресурсная группа и глагол внутри неё.
#:
#: Ключ верхнего уровня — имя группы (ресурс, ед. ч.). Значение — (help
#: группы, {старое плоское имя: новый глагол}). Если группа уже существует
#: (``store``/``role``/``rating``/``comment`` — созданы раньше вручную), она
#: переиспользуется, а её help не трогается.
RESOURCE_GROUPS: dict[str, tuple[str, dict[str, str]]] = {
    "auth": (
        "Аккаунт и сессия: вход/выход, регистрация, вступление, пароль.",
        {
            "login": "login",
            "logout": "logout",
            "whoami": "whoami",
            "passwd": "passwd",
            "register": "register",
            "join": "join",
            "set-tokens": "set-tokens",
        },
    ),
    "skill": (
        "Навыки: поиск, установка, включение, обновление, публикация.",
        {
            "list": "list",
            "show": "show",
            "suggest": "suggest",
            "contributors": "contributors",
            "install": "install",
            "remove": "remove",
            "enable": "enable",
            "disable": "disable",
            "update": "update",
            "installed": "installed",
            "sync": "sync",
            "pull": "pull",
            "push": "push",
            "publish": "publish",
            "report": "report",
            "new": "new",
        },
    ),
    "cli": (
        "Сам CLI: статус, диагностика, журнал, настройки, обновление.",
        {
            "status": "status",
            "doctor": "doctor",
            "logs": "logs",
            "config": "config",
            "upgrade": "upgrade",
        },
    ),
    "device": (
        "Устройства пользователя.",
        {"devices": "list"},
    ),
    "store": (
        "Центральный стор навыков.",
        {"migrate": "migrate"},
    ),
    "member": (
        "Участники компании.",
        {"members": "list"},
    ),
    "role": (
        "Роли и их права.",
        {"roles": "list"},
    ),
    "permission": (
        "Каталог прав платформы.",
        {"permissions": "list"},
    ),
    "comment": (
        "Комментарии к навыкам.",
        {"comments": "list"},
    ),
    "rating": (
        "Рейтинг навыков.",
        {"rate": "set"},
    ),
}

#: Плоские команды, которые ОСТАЮТСЯ плоскими — с обоснованием.
_KEEP_FLAT: dict[str, str] = {
    # Горячий путь: обёртка `skillery run <slug>` вшита в КАЖДЫЙ генерируемый
    # SKILL.md (templates/__init__.py) и вызывается агентом на каждый запуск
    # навыка. Переезд дал бы максимум churn при минимуме пользы.
    "run": "вшито в шаблон SKILL.md, вызывается на каждый запуск навыка",
    # Самостоятельные действия без ресурса-собрата: группа из одного глагола
    # была бы шумом, а не структурой.
    "web": "разовое действие (открыть веб-консоль), ресурса-собрата нет",
    "ask": "разовое действие (LLM-адвайзер), ресурса-собрата нет",
    "onboard": "разовое действие над ПРОЕКТОМ, а не над ресурсом хаба",
    # #2455: `skillery propose <навык>` — единичное действие по имени навыка,
    # ровно как `run`/`ask`. Имя зафиксировано спецификацией приёма
    # предложений (docs/design/skill-proposals.md §10) и попадает в чужие
    # инструкции; заставлять человека писать `proposal create` ради симметрии
    # — плохой обмен. Остальные глаголы предложения живут в группе `proposal`.
    "propose": "единичное действие по имени навыка; имя закреплено спекой",
}


def _warn_stderr(old: str, new: str) -> None:
    """Предупреждение об устаревшем имени — строго в stderr.

    ``emit_message`` здесь НЕ годится: в text-режиме он печатает в **stdout**
    (rich-консоль), а нам нужен stderr в ОБОИХ режимах — stdout может
    парситься даже без ``--json`` (grep/awk по строкам).
    """
    if os.environ.get(SUPPRESS_ENV):
        return
    message = (
        f"Команда `skillery {old}` устарела — используйте "
        f"`skillery {new}`. Старое имя продолжает работать."
    )
    try:
        from skillery_cli import output as _output

        is_json = _output.is_json()
    except Exception:  # noqa: BLE001 — предупреждение не имеет права ронять команду
        is_json = False
    if is_json:
        print(
            json.dumps(
                {
                    "event": "warn",
                    "message": message,
                    "deprecated_command": old,
                    "replacement": new,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return
    from rich.console import Console

    Console(stderr=True).print(f"[yellow]![/] {message}")


def deprecated_alias(
    func: Callable[..., Any], *, old: str, new: str
) -> Callable[..., Any]:
    """Обёртка плоского имени: предупреждение в stderr → вызов ТОЙ ЖЕ функции.

    Сигнатура копируется с оригинала — typer строит по ней те же опции и
    аргументы, поэтому алиас принимает ровно то же, что новая форма.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _warn_stderr(old, new)
        return func(*args, **kwargs)

    wrapper.__signature__ = inspect.signature(func)  # type: ignore[attr-defined]
    return wrapper


def apply_resource_groups(app: typer.Typer) -> list[tuple[str, str]]:
    """Перевести зарегистрированные плоские команды в ресурсные группы.

    Вызывается ПОСЛЕ полной сборки ``build_app``: к этому моменту видно, какие
    команды реально зарегистрированы (часть скрыта permission-гейтами), и
    группа создаётся только под фактически доступные глаголы — иначе в справке
    появлялись бы пустые ресурсы.

    Возвращает список пар ``(старое имя, новое имя)`` — для тестов и отладки.
    """
    groups: dict[str, typer.Typer] = {
        g.name: g.typer_instance for g in app.registered_groups if g.name
    }
    moved: list[tuple[str, str]] = []

    for group_name, (group_help, verbs) in RESOURCE_GROUPS.items():
        pending = [
            (cmd, verbs[cmd.name])
            for cmd in app.registered_commands
            if cmd.name in verbs
        ]
        if not pending:
            continue
        sub = groups.get(group_name)
        if sub is None:
            sub = typer.Typer(no_args_is_help=True, help=group_help)
            app.add_typer(sub, name=group_name)
            groups[group_name] = sub
        taken = {c.name for c in sub.registered_commands}
        for cmd, verb in pending:
            old_name = cmd.name or ""
            new_name = f"{group_name} {verb}"
            # Глагол уже есть в группе (ручная группировка #890) — плоскую
            # форму всё равно депрекейтим, но второй раз не регистрируем.
            if verb not in taken:
                sub.command(
                    name=verb,
                    help=cmd.help,
                    hidden=cmd.hidden,
                )(cmd.callback)
                taken.add(verb)
            # Плоское имя остаётся ВЫЗЫВАЕМЫМ, но уходит из справки и
            # предупреждает при вызове.
            cmd.callback = deprecated_alias(
                cmd.callback, old=old_name, new=new_name
            )
            cmd.hidden = True
            cmd.deprecated = True
            moved.append((old_name, new_name))
    return moved


__all__ = [
    "RESOURCE_GROUPS",
    "SUPPRESS_ENV",
    "apply_resource_groups",
    "deprecated_alias",
]
