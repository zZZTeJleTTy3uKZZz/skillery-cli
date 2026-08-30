"""Блок ``[[capabilities]]`` манифеста навыка (#1489) — чтение и валидация.

Навык ОБЪЯВЛЯЕТ свои способности, а реестр хаба выводится из объявления при
публикации версии — ручного CRUD способностей нет и не будет: реестр, который
можно править отдельно от манифеста, гарантированно разъедется с реальными
entry-point'ами дистрибутива, и разъедется молча.

────────────────────────────────────────────────────────────────────────────
ПОЧЕМУ ПРОВЕРКА ЖИВЁТ И ЗДЕСЬ, ХОТЯ ОНА ЕСТЬ НА БЭКЕНДЕ

Не ради «второго мнения»: правила один в один зеркалят
``domain/skill/manifest.py::_parse_capabilities``, и расходиться им нельзя.
Дело в МОМЕНТЕ. Публикация — длинная: secret-scan, денилист, сборка списка
файлов, сеть. Узнать про опечатку в ``[[capabilities]]`` после всего этого, да
ещё сообщением о 422 с полем ``manifest.capabilities.1.name``, — значит
потерять проход и не понять, что чинить. Проверка до первого байта в сеть
стоит один разбор TOML и говорит на языке файла: строка, имя, что не так.

Второе: ошибку тут МОЖНО объяснить. Бэкенду про ``_skill_meta.toml``
пользователя ничего не известно — он видит DTO; здесь известен и файл, и
номер элемента блока.

────────────────────────────────────────────────────────────────────────────
ДВА РЕЕСТРА ОДНОЙ СУЩНОСТИ: ``[[capabilities]]`` И ``skillery.plugins`` (#2271)

Способность — это и есть плагин: имя способности = ключ entry-point группы
``skillery.plugins`` дистрибутива. Реестров у одной сущности оказалось два, и
они разъехались молча: реальные навыки (telegram, vk-content-cli, notebooklm,
alice-chat-cli, boosty-content-cli) объявляют плагины в ``pyproject.toml``, а
хаб читал только ``[[capabilities]]`` — таких блоков не было НИ ОДНОГО, и в
проде ``capabilities`` и ``capability_leases`` стояли на нуле.

Источник правды выбран **entry-points**, и вот почему. Плагин обязан быть в
``pyproject.toml`` — иначе его не найдёт ``PluginRegistry``, и способности не
существует физически, сколько её ни объявляй в манифесте. Обратное неверно:
манифест без entry-point — это обещание без исполнителя. Значит блок
``[[capabilities]]`` не источник, а **уточнение**: заголовок, описание,
``access_level``, ``requires_lease`` — то, чего в entry-point нет.

Отсюда правило:

* блока нет ⇒ реестр **выводится** из entry-points (дублировать нечего);
* блок есть ⇒ он обязан совпасть с entry-points **по множеству имён**;
  расхождение в любую сторону — ошибка с именем способности и файлом.

``pyproject.toml`` ЧИТАЕТСЯ, а не импортируется — приём
``adapterkit/testing/plugin.py::_pyproject_entry_points``. Публикация не имеет
права выполнять код публикуемого навыка: у него свои зависимости, свои
побочные эффекты при импорте и, вообще говоря, чужой автор.

────────────────────────────────────────────────────────────────────────────
ГЛОБАЛЬНАЯ УНИКАЛЬНОСТЬ ИМЕНИ — НЕ ЛОКАЛЬНАЯ ПРОВЕРКА

Дубль ВНУТРИ одного манифеста ловится здесь. А вот занятость имени ДРУГИМ
навыком локально не проверить: имя уникально на весь хаб, и знает об этом
только хаб (409 ``CAPABILITY_NAME_CONFLICT``). Задача CLI — не спрятать этот
ответ за кодом ошибки, а объяснить его человеку; текст живёт в
``commands/capability.py::NAME_CONFLICT_HINT``, чтобы не разъехаться на две
формулировки.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

#: Группа entry-point'ов, в которой живут плагины Skillery. Объявляют её
#: ``adapterkit/registry.py`` и ``socialkit/registry.py``, читает ``gateway``.
#: Здесь — та же строка, но БЕЗ зависимости на киты: публикации нужны две
#: проверки чтения TOML, а не инфраструктура вызова плагинов.
PLUGIN_ENTRY_POINT_GROUP = "skillery.plugins"

#: Докуда подниматься в поисках ``pyproject.toml``. Навык обычно лежит в
#: ``<repo>/skills/<name>/``, а дистрибутив описан в корне репозитория —
#: поэтому подъём нужен. Но он обязан быть ОГРАНИЧЕН: без границы публикация
#: папки, случайно оказавшейся внутри чужого проекта, приписала бы навыку
#: чужие плагины. Граница — корень репозитория (``.git``), запасная — глубина.
PYPROJECT_SEARCH_DEPTH = 4

#: Уровни доступа способности (зеркало ``_STRICTNESS`` бэкенда).
ACCESS_LEVELS = ("public", "restricted", "private")

#: Ключи, которые понимает бэкенд. Остальное — опечатка автора, и молчать о
#: ней нельзя: незнакомый ключ уедет в никуда, а автор будет уверен, что
#: объявил ``requires_lease``, написав ``require_lease``.
KNOWN_KEYS = frozenset(
    {
        "name",
        "entry_point",
        # #1268: группа entry-point'ов и шаг доустановки. Бэкенд их принимает
        # (``CapabilityDecl.entry_point_group``/``setup``), а этот гейт — нет:
        # автор писал их в манифест, публикация падала с «неизвестные ключи», и
        # единственным выходом было УБРАТЬ поля, которые хаб на самом деле ждёт.
        "entry_point_group",
        "setup",
        "kind",
        "title",
        "description",
        "access_level",
        "requires_lease",
    }
)


class CapabilityManifestError(ValueError):
    """Блок ``[[capabilities]]`` непригоден к публикации."""


def parse_capabilities(meta_toml: dict[str, Any]) -> list[dict[str, Any]]:
    """``[[capabilities]]`` → список DTO для публикации. Строго, без best-effort.

    В отличие от ``[[hooks]]``/``onboarding`` (там мусор молча отбрасывается,
    чтобы кривой необязательный блок не ронял установку всего навыка) здесь
    выбрана противоположная строгость, и по той же причине, что у бэкенда:
    способность без имени — это способность без адреса. Её нельзя ни выдать,
    ни отозвать, ни зарезолвить в лизе, и «молча пропустить» означало бы
    опубликовать версию, в которой обещанной способности просто нет.
    """
    raw = meta_toml.get("capabilities")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CapabilityManifestError(
            "[[capabilities]] должен быть массивом таблиц: "
            "[[capabilities]] name = \"grok_transcriber\""
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        where = f"[[capabilities]] #{position}"
        if not isinstance(item, dict):
            raise CapabilityManifestError(f"{where}: элемент должен быть таблицей")
        unknown = sorted(set(item) - KNOWN_KEYS)
        if unknown:
            raise CapabilityManifestError(
                f"{where}: неизвестные ключи {', '.join(unknown)}; "
                f"допустимо: {', '.join(sorted(KNOWN_KEYS))}"
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise CapabilityManifestError(
                f"{where}: нет обязательного 'name'. Имя способности = ключ "
                "entry-point группы skillery.plugins (grok_transcriber)"
            )
        if name in seen:
            raise CapabilityManifestError(
                f"{where}: дубль имени {name!r} в одном манифесте — "
                "останется только одна строка, и неизвестно какая"
            )
        seen.add(name)
        access_level = item.get("access_level")
        if access_level is not None and str(access_level) not in ACCESS_LEVELS:
            raise CapabilityManifestError(
                f"{where}: access_level {access_level!r} невалиден; "
                f"допустимо: {', '.join(ACCESS_LEVELS)}"
            )
        entry: dict[str, Any] = {"name": name}
        for key in (
            "entry_point",
            "entry_point_group",
            "setup",
            "kind",
            "title",
            "description",
        ):
            value = item.get(key)
            if value:
                entry[key] = str(value)
        if access_level:
            entry["access_level"] = str(access_level)
        entry["requires_lease"] = bool(item.get("requires_lease", False))
        out.append(entry)
    return out


def entry_point_groups_of(
    skill_dir: Path | str,
) -> tuple[dict[str, dict[str, str]], Path | None]:
    """ВСЕ группы entry-point'ов дистрибутива навыка: ``{группа: {имя: адрес}}``.

    Групп стало больше одной (#1268): способность может жить в реестре
    стороннего потребителя, и её ``entry_point_group`` указан в манифесте.
    Чтобы сверить объявление с реальностью, надо видеть не только
    ``skillery.plugins``, но и ту группу, которую назвал автор — иначе
    единственным исходом сверки было бы «плагина нет», что неправда.

    Читаем ``pyproject.toml``, а не импортируем пакет: публикация не исполняет
    код публикуемого навыка (см. :func:`plugin_entry_points_of`).
    """
    start = Path(skill_dir).resolve()
    chain = [start, *start.parents][: PYPROJECT_SEARCH_DEPTH + 1]
    for directory in chain:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
                # Битый чужой pyproject не должен ронять публикацию навыка:
                # групп мы из него не узнали — значит и сверять нечего.
                return {}, None
            raw = (data.get("project", {}) or {}).get("entry-points") or {}
            if not isinstance(raw, dict):
                return {}, None
            groups: dict[str, dict[str, str]] = {}
            for group, table in raw.items():
                if isinstance(table, dict):
                    groups[str(group)] = {
                        str(name): str(value) for name, value in table.items()
                    }
            return groups, candidate
        if (directory / ".git").exists():
            # Корень репозитория без pyproject — выше уже чужая территория.
            break
    return {}, None


def plugin_entry_points_of(skill_dir: Path | str) -> tuple[dict[str, str], Path | None]:
    """Плагины ``skillery.plugins`` дистрибутива навыка: ``{имя: "модуль:Класс"}``.

    ЧИТАЕМ ``pyproject.toml``, а не импортируем пакет (приём
    ``adapterkit/testing/plugin.py::_pyproject_entry_points``). Публикация не
    исполняет код публикуемого навыка ни при каких условиях: у него свои
    зависимости, свои побочные эффекты импорта и чужой автор. Плюс это
    работает на пакете, который ещё не установлен, — а при публикации он как
    раз обычно не установлен.

    Возвращает ещё и путь к найденному файлу: сообщение об ошибке обязано
    называть КОНКРЕТНЫЙ файл, а не «ваш pyproject».
    """
    groups, candidate = entry_point_groups_of(skill_dir)
    return dict(groups.get(PLUGIN_ENTRY_POINT_GROUP) or {}), candidate


def reconcile_capabilities(
    declared: list[dict[str, Any]],
    entry_points: dict[str, str],
    *,
    manifest_path: Path | None = None,
    pyproject_path: Path | None = None,
    groups: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Свести объявление манифеста с реальными плагинами дистрибутива (#2271).

    Источник правды — entry-points: плагина, которого нет в ``pyproject.toml``,
    не существует физически (``PluginRegistry`` его не найдёт), сколько бы
    манифест его ни обещал. Блок ``[[capabilities]]`` — уточнение поверх:
    заголовок, описание, ``access_level``, ``requires_lease``.

    * ``pyproject.toml`` не найден ⇒ сверять не с чем: возвращаем объявленное
      как есть. Так навык-инструкция (без дистрибутива вовсе) и навык, лежащий
      вне репозитория, не получают ложного срабатывания;
    * блок пуст ⇒ **выводим** реестр из entry-points. Именно это и было
      сломано: сегодня такие способности не видит никто;
    * блок непуст ⇒ множества имён обязаны совпасть. Расхождение в любую
      сторону — ошибка с именем способности и файлом.

    #1268: способность может жить не в ``skillery.plugins``, а в группе
    стороннего потребителя (``entry_point_group``). Такая строка сверяется со
    СВОЕЙ группой, а не с дефолтной: иначе честное объявление выглядело бы как
    «плагина нет». Расхождение группы — отдельная ошибка, потому что цена у неё
    та же, что у отсутствующего плагина: потребитель резолвит способность по
    паре (группа, имя) и в чужой группе её не найдёт.
    """
    if pyproject_path is None and not entry_points and not groups:
        return declared
    if not declared:
        return [
            {
                "name": name,
                "entry_point": entry_points[name],
                "requires_lease": False,
            }
            for name in sorted(entry_points)
        ]
    where_manifest = str(manifest_path or "_skill_meta.toml")
    where_pyproject = str(pyproject_path or "pyproject.toml")
    known_groups = groups or {}

    # #1268: строки с ЧУЖОЙ группой проверяются отдельно и в общий
    # symmetric-difference по ``skillery.plugins`` не входят — их там и не
    # должно быть.
    foreign: list[dict[str, Any]] = []
    default_declared: list[dict[str, Any]] = []
    for item in declared:
        group = str(item.get("entry_point_group") or "").strip()
        if group and group != PLUGIN_ENTRY_POINT_GROUP:
            foreign.append(item)
        else:
            default_declared.append(item)

    for item in foreign:
        name = str(item["name"])
        group = str(item["entry_point_group"])
        table = known_groups.get(group)
        if table is None:
            # Группы нет в дистрибутиве вовсе. Молчать нельзя: потребитель
            # ищет способность по паре (группа, имя) и не найдёт ничего, а
            # автор будет уверен, что опубликовал рабочую способность.
            available = ", ".join(sorted(known_groups)) or "ни одной"
            raise CapabilityManifestError(
                f"{where_manifest}: способность {name} объявлена в группе "
                f"entry-point {group!r}, но такой группы нет в "
                f"[project.entry-points] файла {where_pyproject} "
                f"(там объявлены: {available}). Потребитель резолвит "
                "способность по паре (группа, имя) и в чужой группе её не "
                "найдёт."
            )
        if name not in table:
            raise CapabilityManifestError(
                f"{where_manifest}: способности {name} нет в "
                f'[project.entry-points."{group}"] файла {where_pyproject}. '
                "Способность без плагина — обещание без исполнителя."
            )

    declared_names = {str(item["name"]) for item in default_declared}
    if not declared_names and foreign:
        # Весь блок — чужие группы: сверять с ``skillery.plugins`` нечего,
        # иначе каждая такая способность выглядела бы как «не объявлена».
        return _with_entry_points(declared, entry_points, known_groups)
    missing_plugin = sorted(declared_names - set(entry_points))
    if missing_plugin:
        raise CapabilityManifestError(
            f"{where_manifest}: способности {', '.join(missing_plugin)} объявлены "
            f"в [[capabilities]], но их нет в [project.entry-points."
            f'"{PLUGIN_ENTRY_POINT_GROUP}"] файла {where_pyproject}. '
            "Способность без плагина — обещание без исполнителя: реестр хаба "
            "заведёт имя, а резолв на устройстве не найдёт ничего."
        )
    missing_declaration = sorted(set(entry_points) - declared_names)
    if missing_declaration:
        raise CapabilityManifestError(
            f"{where_pyproject}: плагины {', '.join(missing_declaration)} объявлены "
            f'в [project.entry-points."{PLUGIN_ENTRY_POINT_GROUP}"], но их нет в '
            f"[[capabilities]] файла {where_manifest}. Раз блок заполняется "
            "вручную — он обязан покрывать ВЕСЬ дистрибутив, иначе часть "
            "способностей уедет в хаб, а часть останется невидимой."
        )
    # Имена сошлись — дополняем адресом плагина то, что его не указало.
    return _with_entry_points(declared, entry_points, known_groups)


def _with_entry_points(
    declared: list[dict[str, Any]],
    entry_points: dict[str, str],
    groups: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Дописать адрес плагина туда, где автор его не указал.

    Адрес берётся из ТОЙ группы, которую назвала сама способность: подставить
    сюда адрес из ``skillery.plugins`` значило бы подменить исполнителя.
    """
    out: list[dict[str, Any]] = []
    for item in declared:
        entry = dict(item)
        group = str(entry.get("entry_point_group") or "").strip()
        table = groups.get(group, {}) if group else entry_points
        address = table.get(str(entry["name"]))
        if address:
            entry.setdefault("entry_point", address)
        out.append(entry)
    return out


def capabilities_of(skill_dir: Path | str) -> list[dict[str, Any]]:
    """Способности навыка: ``[[capabilities]]``, сверенные с плагинами (#2271).

    Нет файла (prompt/comprehensive-навык) ⇒ объявления нет, но реестр всё
    равно выводится из ``skillery.plugins`` дистрибутива, если он есть: у
    навыков вроде ``boosty-content-cli`` и ``alice-chat-cli`` манифеста нет
    вовсе, а плагины есть — и раньше хаб не узнавал о них ничего. Нет ни
    манифеста, ни ``pyproject.toml`` ⇒ пусто. Битый TOML ⇒ ошибка: его всё
    равно не переживёт сборка манифеста, но здесь сообщение называет файл.
    """
    skill_dir = Path(skill_dir)
    path = skill_dir / "_skill_meta.toml"
    groups, pyproject_path = entry_point_groups_of(skill_dir)
    entry_points = dict(groups.get(PLUGIN_ENTRY_POINT_GROUP) or {})
    if not path.is_file():
        return reconcile_capabilities(
            [],
            entry_points,
            manifest_path=path,
            pyproject_path=pyproject_path,
            groups=groups,
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise CapabilityManifestError(f"{path} не парсится: {exc}") from exc
    return reconcile_capabilities(
        parse_capabilities(data),
        entry_points,
        manifest_path=path,
        pyproject_path=pyproject_path,
        groups=groups,
    )


__all__ = [
    "ACCESS_LEVELS",
    "KNOWN_KEYS",
    "PLUGIN_ENTRY_POINT_GROUP",
    "CapabilityManifestError",
    "capabilities_of",
    "entry_point_groups_of",
    "parse_capabilities",
    "plugin_entry_points_of",
    "reconcile_capabilities",
]
