"""чистые рендер-функции шаблонов навыка (scaffold).

ВНИМАНИЕ: ЭТО ПОСЛЕДНЯЯ КОПИЯ КАНОНА, И ОНА ВРЕМЕННАЯ.

Канон раскладки навыка сведён в ОДИН источник — ``skillkit.canon``
(кит ``s-skillkit`` с 0.4.0; обоснование — ADR-0001 кита). Копия в ``clikit``
удалена, копия фабрики (``reverse_factory.factory.canon``) стала тонким
ре-экспортом. Здесь копия ещё живёт: перевод меняет то, что видит пользователь
команды ``skillery new`` (раскладка ``skills/<slug>/`` + ``src/<пакет>/`` +
``install/`` + ``tests/`` вместо папки навыка со ``scripts/``), и требует
расширить ``canon.SkillMeta`` под ``[[mcp]]``. Работа заведена задачей Atlas
#2537 (проект ``skills-hub``).

ПОКА ОНА НЕ СДЕЛАНА: не дописывай сюда новых знаний о ФОРМЕ навыка и не копируй
их из кита. Правишь форму — правь ``skillkit.canon``. Расхождение, из-за которого
всё и затевалось: ``scripts/install_skill.py``, который рендерит этот модуль,
канон числит артефактом СТАРОЙ топологии (``canon.LEGACY_ARTEFACTS``) и
отвергает; замена — ``install/`` из ``canon.render_install_docs``.

Как устроено сейчас. Шаблоны — чистые строки с ``{{token}}``-подстановкой,
рендереры отделены от записи на диск (она в ``commands/scaffold.py``).
Зависимостей вне stdlib нет намеренно (frontmatter/TOML генерируем сами).

Что рендерим (по типу навыка ``kind``):

- ``render_skill_md`` — ``SKILL.md`` с валидным frontmatter (name/description/
  version) + телом по AK-канону (router + 5 секций, double-quoted description,
  ``Respond in the user's language.``). Тело адаптируется под kind. #1221: во
  всех kind'ах инструкция ведёт вызов через обёртку ``skillery run <slug>`` —
  учёт использования делает КОД обёртки, а не обещание модели отчитаться
  (просить LLM «сообщить о вызове» ненадёжно: отчёт зависит от того, вспомнит
  ли она).
- ``render_readme`` — короткий README навыка.
- ``render_references_stub`` — заглушка ``references/`` для comprehensive.
- ``render_skill_meta_toml`` — ``_skill_meta.toml`` по схеме
  (``kind`` + ``[[cli]]`` / ``[[mcp]]`` + ``runtime_dependencies``).
- ``render_install_skill_py`` / ``render_self_check_py`` /
  ``render_smoke_test_py`` — онбординг-триада скриптов (кроссплатформ
  ``{home}``-шаблон, ``--with-deps`` uv→pip fallback, pass/warn/fail + ``--strict``,
  graceful degradation).
- ``render_pyproject`` — ``pyproject.toml`` с entry-point для tooling-CLI.
- ``render_tooling_cli_module`` / ``render_cli_init`` — встроенный CLI-скелет на
  ``clikit`` (зеркалит ``clikit.scaffold.render_cli_module``; clikit не является
  зависимостью клиента, поэтому скелет встроен текстом, а clikit объявляется
  зависимостью СГЕНЕРЁННОГО навыка).
"""
from __future__ import annotations

import re

# Допустимые типы навыка (зеркало backend SkillKind).
SKILL_KINDS: tuple[str, ...] = ("prompt", "comprehensive", "tooling")

#: Версия по умолчанию для нового навыка.
DEFAULT_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
#  Идентификаторы / нормализация
# ---------------------------------------------------------------------------
def module_name(slug: str) -> str:
    """Python-идентификатор пакета из slug (для ``[project.scripts]`` и CLI).

    Зеркалит ``clikit.scaffold.package_name`` / ``skill_emitter._module_name``:
    дефисы/пробелы/точки → ``_``; прочие нелегальные символы убираются; ведущая
    цифра префиксуется ``_`` (``2fa`` → ``_2fa``). Пустой → ``skill``.
    """
    ident = re.sub(r"[^0-9a-z_]+", "_", slug.lower()).strip("_")
    if not ident:
        return "skill"
    if ident[0].isdigit():
        ident = f"_{ident}"
    return ident


def _yaml_double_quote(value: str) -> str:
    """Завернуть строку в двойные кавычки с экранированием ``\\`` и ``"`` (AK-канон)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_quote(value: str) -> str:
    """TOML basic-string: двойные кавычки + экранирование ``\\`` и ``"``."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render(template: str, substitutions: dict[str, str]) -> str:
    """Подставить все ``{{token}}``-плейсхолдеры; неизвестные оставить как есть."""
    out = template
    for placeholder, value in substitutions.items():
        out = out.replace(placeholder, value)
    return out


# ---------------------------------------------------------------------------
#  SKILL.md — frontmatter + тело по AK-канону (router + 5 секций)
# ---------------------------------------------------------------------------
_FRONTMATTER_TEMPLATE = """---
name: {{slug}}
version: {{version}}
kind: {{kind}}
description: {{description_quoted}}
---
"""

_BODY_COMMON_HEADER = """
# {{title}}

Respond in the user's language.

{{intro}}

## Route by request type

| Запрос (request) | Куда смотреть (where) |
|------------------|------------------------|
{{router_rows}}

## When activated

Используй этот навык, когда нужно:

{{when_bullets}}

НЕ используй, если задача не про этот навык — выбери профильный.

## Instructions

{{instructions}}

## Examples

{{examples}}

## Rules

{{rules}}
{{extra_sections}}"""


def _intro_for_kind(kind: str, title: str) -> str:
    if kind == "tooling":
        return (
            f"Tooling-навык «{title}»: несёт исполняемые артефакты (CLI и/или "
            "MCP-сервер) и онбординг-триаду (`scripts/install_skill.py`, "
            "`scripts/self_check.py`, `SKILL.md`). Всё нужное — внутри папки навыка."
        )
    if kind == "comprehensive":
        return (
            f"Comprehensive-навык «{title}»: инструкция с кейсами/примерами и "
            "справочными материалами в `references/`."
        )
    return f"Промпт-навык «{title}»: чистая инструкция для ИИ-агента."


def _router_rows_for_kind(kind: str, slug: str) -> str:
    rows = [
        "| Понять, когда применять | секция «When activated» |",
        "| Пошаговое выполнение | секция «Instructions» |",
        f"| Запустить навык | `skillery run {slug} …` (обёртка учёта) |",
    ]
    if kind == "comprehensive":
        rows.append("| Детали и кейсы | `references/` |")
    if kind == "tooling":
        rows.append("| Установить навык в агента | `scripts/install_skill.py --agent <name>` |")
        rows.append("| Проверить окружение | `scripts/self_check.py` |")
    return "\n".join(rows)


def _when_bullets_for_kind(kind: str, title: str) -> str:
    bullets = [f"- решить задачу из домена «{title}»;", "- следовать каноничному процессу навыка;"]
    if kind == "tooling":
        bullets.append("- запустить готовую CLI-/MCP-автоматизацию навыка;")
    if kind == "comprehensive":
        bullets.append("- свериться с примерами/кейсами в `references/`;")
    return "\n".join(bullets)


def _instructions_for_kind(kind: str, slug: str) -> str:
    if kind == "tooling":
        return (
            "1. Поставь навык в агента: `python scripts/install_skill.py --agent claude-code`.\n"
            "2. Проверь окружение: `python scripts/self_check.py`.\n"
            "3. Установи зависимости (если есть CLI): `python scripts/install_skill.py "
            "--agent claude-code --with-deps` (uv→pip).\n"
            f"4. Запускай инструмент навыка ЧЕРЕЗ обёртку: `skillery run {slug} "
            "<аргументы>` — аргументы и код возврата проходят насквозь, а факт "
            "вызова фиксируется автоматически."
        )
    if kind == "comprehensive":
        return (
            "1. Определи подзадачу и сверься с router-таблицей выше.\n"
            "2. При необходимости открой нужный файл из `references/`.\n"
            f"3. Отметь применение навыка: `skillery run {slug}`.\n"
            "4. Следуй пошаговому процессу секции и примерам ниже."
        )
    return (
        "1. Определи подзадачу и сверься с router-таблицей выше.\n"
        f"2. Отметь применение навыка: `skillery run {slug}`.\n"
        "3. Следуй процессу и примерам ниже.\n"
        "4. Сформулируй результат на языке пользователя."
    )


def _examples_for_kind(kind: str, title: str) -> str:
    if kind == "tooling":
        return (
            f"- Типовой: установка навыка `{title}` в Claude Code и self-check.\n"
            "- Краевой: окружение новое → сначала `self_check.py`, он подскажет, "
            "чего не хватает (graceful degradation)."
        )
    return (
        f"- Типовой: базовый сценарий применения навыка «{title}».\n"
        "- Краевой: нестандартный ввод — опиши, как навык деградирует/уточняет."
    )


def _rules_for_kind(kind: str, slug: str) -> str:
    rules = [
        "- Отвечай на языке пользователя; тело навыка не переводи дословно.",
        "- Не выходи за границы домена навыка — для смежных задач выбери другой навык.",
        f"- Вызывай навык через `skillery run {slug}` — учёт использования делает "
        "обёртка, отчитываться о вызове текстом не нужно.",
    ]
    if kind == "tooling":
        rules.append("- Все скрипты/CLI остаются внутри папки навыка (переносимость).")
        rules.append(
            "- Новые зависимости — в `pyproject.toml` / `_skill_meta.toml`, "
            "не в системный Python."
        )
        rules.append("- Секреты/сессии в навык не кладём.")
    return "\n".join(rules)


def _extra_sections_for_kind(kind: str) -> str:
    if kind == "comprehensive":
        return (
            "\n## References\n\n"
            "- `references/overview.md` — обзор домена и ключевые понятия.\n"
            "- `references/cases.md` — разобранные кейсы/примеры (замени своими).\n"
        )
    if kind == "tooling":
        return (
            "\n## Bundled resources\n\n"
            "- `_skill_meta.toml` — манифест (kind=tooling, [[cli]]/[[mcp]], "
            "runtime_dependencies).\n"
            "- `scripts/install_skill.py` — установка в каталог агента "
            "(--with-deps uv→pip).\n"
            "- `scripts/self_check.py` — проверка окружения (pass/warn/fail, --strict).\n"
            "- `scripts/smoke_test.py` — быстрый smoke навыка.\n"
        )
    return ""


def render_skill_md(
    *,
    slug: str,
    kind: str,
    description: str,
    version: str = DEFAULT_VERSION,
    with_cli: bool = False,
    with_mcp: bool = False,
    title: str | None = None,
) -> str:
    """Собрать ``SKILL.md``: frontmatter + тело по AK-канону под ``kind``.

    ``description`` пишется в двойных кавычках (AK-канон). Тело — router-таблица
    + 5 секций (When activated / Instructions / Examples / Rules) + kind-зависимые
    доп. секции (References для comprehensive, Bundled resources для tooling).
    ``with_cli``/``with_mcp`` влияют только на упоминания артефактов в теле.
    """
    _ = with_cli, with_mcp  # влияют на текст через kind=tooling-ветку
    display_title = title or slug
    frontmatter = _render(
        _FRONTMATTER_TEMPLATE,
        {
            "{{slug}}": slug,
            "{{version}}": version,
            "{{kind}}": kind,
            "{{description_quoted}}": _yaml_double_quote(description),
        },
    )
    body = _render(
        _BODY_COMMON_HEADER,
        {
            "{{title}}": display_title,
            "{{intro}}": _intro_for_kind(kind, display_title),
            "{{router_rows}}": _router_rows_for_kind(kind, slug),
            "{{when_bullets}}": _when_bullets_for_kind(kind, display_title),
            "{{instructions}}": _instructions_for_kind(kind, slug),
            "{{examples}}": _examples_for_kind(kind, display_title),
            "{{rules}}": _rules_for_kind(kind, slug),
            "{{extra_sections}}": _extra_sections_for_kind(kind),
        },
    )
    return frontmatter + body


# ---------------------------------------------------------------------------
#  README + references-заглушка
# ---------------------------------------------------------------------------
def render_readme(*, slug: str, kind: str, description: str) -> str:
    """Короткий README навыка под структуру, которую кладёт scaffold."""
    pkg = module_name(slug)
    lines = [
        f"# {slug}",
        "",
        description,
        "",
        f"- тип навыка: `{kind}`",
        f"- версия: `{DEFAULT_VERSION}`",
        "",
        "## Структура",
        "",
        "- `SKILL.md` — инструкция для ИИ-агента (router + 5 секций по AK-канону).",
        "- `scripts/install_skill.py` — установка навыка в каталог агента (онбординг-триада).",
        "- `scripts/self_check.py` — проверка окружения (pass/warn/fail, `--strict`).",
    ]
    if kind == "comprehensive":
        lines.append("- `references/` — справочные материалы и кейсы.")
    if kind == "tooling":
        lines += [
            "- `_skill_meta.toml` — манифест навыка (kind=tooling, [[cli]]/[[mcp]]).",
            "- `scripts/smoke_test.py` — быстрый smoke навыка.",
            f"- `{pkg}/cli.py` — точка входа CLI навыка (на базе clikit).",
            "- `pyproject.toml` — зависимости и консольный entry-point.",
        ]
    lines += [
        "",
        "## Установка в ИИ-агента",
        "",
        "```bash",
        "python scripts/install_skill.py --agent claude-code",
        "python scripts/self_check.py",
        "```",
        "",
        "## Вызов навыка",
        "",
        "Навык вызывается через обёртку CLI — она фиксирует факт запуска",
        "(аргументы и код возврата проходят насквозь):",
        "",
        "```bash",
        f"skillery run {slug} [аргументы]",
        "```",
        "",
        "## Публикация в Skillery",
        "",
        "```bash",
        "skillery install --path . --scope global",
        "```",
        "",
    ]
    return "\n".join(lines)


def render_references_stub(*, slug: str, name: str) -> str:
    """Заглушка одного файла в ``references/`` (comprehensive)."""
    if name == "overview.md":
        return (
            f"# {slug} — обзор\n\n"
            "Опиши здесь домен навыка, ключевые понятия и контекст.\n"
            "Это заглушка — замени реальным справочным материалом.\n"
        )
    return (
        f"# {slug} — кейсы и примеры\n\n"
        "Собери здесь разобранные кейсы/примеры применения навыка.\n"
        "Это заглушка — замени реальными кейсами.\n"
    )


# ---------------------------------------------------------------------------
#  _skill_meta.toml — схема
# ---------------------------------------------------------------------------
def render_skill_meta_toml(
    *,
    slug: str,
    description: str,
    with_cli: bool = False,
    with_mcp: bool = False,
    version: str = DEFAULT_VERSION,
) -> str:
    """Сгенерировать ``_skill_meta.toml`` по схеме (kind=tooling).

    Кладёт ``description`` / ``version`` / ``kind="tooling"`` и — по флагам —
    секции ``[[cli]]`` (command_name=slug, entrypoint на пакет навыка) и
    ``[[mcp]]`` (transport=stdio). ``runtime_dependencies`` всегда присутствует
    (пустой массив + закомментированный пример), чтобы автор видел, куда дописать.
    Результат парсится stdlib ``tomllib`` и проходит backend парсер.
    """
    pkg = module_name(slug)
    lines = [
        "# Манифест навыка. Backend читает его при `skillery publish`.",
        f"description = {_toml_quote(description)}",
        f'version = "{version}"',
        "",
        "# Тип навыка: prompt | comprehensive | tooling.",
        'kind = "tooling"',
        "",
        "# Триггеры/теги для каталога (заполни своими).",
        "triggers = []",
        "tags = []",
        "",
        "# Runtime-зависимости ПАКЕТОВ (kind: pip|npm|system). Пример:",
        '#   { kind = "pip", spec = "httpx>=0.27" },',
        "runtime_dependencies = []",
    ]
    if with_cli:
        lines += [
            "",
            "# CLI-инструмент(ы), которые несёт навык. Каждый — отдельная [[cli]].",
            "[[cli]]",
            f"command_name = {_toml_quote(slug)}",
            f'entrypoint = "{pkg}.cli:app"',
        ]
    if with_mcp:
        lines += [
            "",
            "# MCP-сервер(ы), которые несёт навык. Каждый — отдельная [[mcp]].",
            "[[mcp]]",
            f"server_name = {_toml_quote(slug + '-mcp')}",
            'transport = "stdio"',
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
#  Онбординг-триада: install_skill / self_check / smoke_test
# ---------------------------------------------------------------------------
_INSTALL_SKILL_TEMPLATE = '''#!/usr/bin/env python3
"""Установщик навыка {{slug}} в каталог ИИ-агента (онбординг-триада).

Кроссплатформенный (stdlib): копирует папку навыка в каталог skills выбранного
агента по шаблону ``{home}``. Опционально ставит python-зависимости навыка
(``--with-deps``: uv→pip fallback) и прогоняет self-check. Идемпотентен:
``--overwrite`` перезаписывает существующую установку.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_NAME = {{slug_repr}}

# Каталоги skills по агентам. ``{home}`` подставляется реальным домашним каталогом
# (кроссплатформа: на Windows слэши нормализуются). Пустой шаблон → нужен --target-dir.
SUPPORTED_AGENTS = {
    "claude-code": "{home}/.claude/skills",
    "codex": "{home}/.codex/skills",
    "opencode": "{home}/.config/opencode/skills",
    "generic": "",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Установить навык в каталог ИИ-агента.")
    parser.add_argument(
        "--agent",
        choices=sorted(SUPPORTED_AGENTS) + ["custom"],
        required=True,
        help="Целевой ИИ-агент (custom требует --target-dir).",
    )
    parser.add_argument("--target-dir", default="", help="Явный каталог skills.")
    parser.add_argument("--overwrite", action="store_true", help="Перезаписать установку.")
    parser.add_argument(
        "--with-deps",
        action="store_true",
        help="Поставить зависимости навыка (uv sync → pip install -e . fallback).",
    )
    parser.add_argument(
        "--run-self-check",
        action="store_true",
        help="После установки прогнать scripts/self_check.py.",
    )
    return parser.parse_args()


def resolve_target_root(agent: str, target_dir: str) -> Path:
    if target_dir:
        return Path(target_dir).expanduser().resolve()
    if agent == "custom":
        raise SystemExit("Для custom нужно передать --target-dir.")
    template = SUPPORTED_AGENTS[agent]
    if not template:
        raise SystemExit(f"Для агента {agent} задай --target-dir.")
    home = str(Path.home()).replace("\\\\", "/")
    return Path(template.replace("{home}", home)).expanduser().resolve()


def install_deps(skill_dir: Path) -> None:
    """Поставить зависимости навыка: uv → pip fallback, graceful degradation."""
    if shutil.which("uv"):
        rc = subprocess.run(["uv", "sync"], cwd=skill_dir, check=False).returncode
        if rc == 0:
            return
        print("uv sync не удался — пробую uv pip install -e .", flush=True)
        rc = subprocess.run(
            ["uv", "pip", "install", "-e", "."], cwd=skill_dir, check=False
        ).returncode
        if rc == 0:
            return
        print("uv недоступен/упал — деградирую на pip.", flush=True)
    rc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."], cwd=skill_dir, check=False
    ).returncode
    if rc != 0:
        print("Зависимости не установлены (нет uv/pip или ошибка). См. вывод выше.", flush=True)


def main() -> None:
    args = parse_args()
    skill_root = Path(__file__).resolve().parents[1]
    target_root = resolve_target_root(args.agent, args.target_dir)
    target_root.mkdir(parents=True, exist_ok=True)
    target_dir = target_root / SKILL_NAME

    if target_dir.exists():
        if not args.overwrite:
            raise SystemExit(f"Навык уже установлен: {target_dir}. Используй --overwrite.")
        shutil.rmtree(target_dir)

    shutil.copytree(
        skill_root,
        target_dir,
        ignore=shutil.ignore_patterns(".venv", "venv", "__pycache__", ".git", "*.pyc"),
    )
    print("Навык установлен в:", target_dir, flush=True)

    if args.with_deps:
        install_deps(target_dir)
    if args.run_self_check:
        subprocess.run(
            [sys.executable, str(target_dir / "scripts" / "self_check.py")], check=False
        )


if __name__ == "__main__":
    main()
'''

_SELF_CHECK_TEMPLATE = '''#!/usr/bin/env python3
"""Self-check навыка {{slug}}: проверка окружения и целостности.

Печатает отчёт pass/warn/fail и завершается ненулевым кодом при критических
провалах. ``--strict`` считает WARN провалом (для CI). Только stdlib,
кроссплатформенный, graceful degradation (мягкие WARN не валят прогон).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 11)


class Result:
    """Один пункт проверки. level: 'pass' | 'warn' | 'fail'."""

    def __init__(self, name: str, level: str, detail: str = "") -> None:
        self.name = name
        self.level = level
        self.detail = detail


def ok(name: str, detail: str = "") -> Result:
    return Result(name, "pass", detail)


def warn(name: str, detail: str = "") -> Result:
    return Result(name, "warn", detail)


def fail(name: str, detail: str = "") -> Result:
    return Result(name, "fail", detail)


def check_python() -> Result:
    cur = sys.version.split()[0]
    if sys.version_info >= MIN_PYTHON:
        return ok("Python >= 3.11", f"найден {cur}")
    return fail("Python >= 3.11", f"текущий {cur} — обнови интерпретатор")


def check_files() -> list[Result]:
    required = [SKILL_ROOT / "SKILL.md"]
    results: list[Result] = []
    for path in required:
        if path.is_file():
            results.append(ok(f"file {path.name}", str(path)))
        else:
            results.append(fail(f"file {path.name}", f"{path} не найден"))
    return results


def check_package_manager() -> Result:
    if shutil.which("uv"):
        return ok("Менеджер пакетов", "uv найден")
    return warn("Менеджер пакетов", "uv не найден — будет использован pip (медленнее)")


def run_checks() -> list[Result]:
    results = [check_python()]
    results += check_files()
    results.append(check_package_manager())
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self-check навыка {{slug}}.")
    parser.add_argument("--strict", action="store_true", help="WARN считать провалом (CI).")
    args = parser.parse_args(argv)

    results = run_checks()
    mark = {"pass": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    width = max((len(r.name) for r in results), default=10) + 2
    print("=== {{slug}} · self-check ===")
    for r in results:
        print(f"  {mark[r.level]} {r.name.ljust(width)} {r.detail}")

    fails = [r for r in results if r.level == "fail"]
    warns = [r for r in results if r.level == "warn"]
    if fails:
        print(f"ПРОВАЛ: {len(fails)} критических проверок не прошли.")
        return 1
    if warns and args.strict:
        print(f"STRICT: {len(warns)} предупреждений — в strict-режиме это провал.")
        return 1
    suffix = f" ({len(warns)} WARN)" if warns else ""
    print(f"OK: окружение готово{suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_SMOKE_TEST_TEMPLATE = '''#!/usr/bin/env python3
"""Smoke-test навыка {{slug}}: быстрая проверка, что навык на месте.

Минимальный сигнал «навык не сломан»: self-check проходит. Только stdlib.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(SKILL_ROOT / "scripts" / "self_check.py")], check=False
    )
    if result.returncode != 0:
        print("Smoke-test провален: self-check вернул ненулевой код.", flush=True)
        return 1
    print("Smoke-test пройден.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def render_install_skill_py(*, slug: str) -> str:
    """Сгенерировать ``scripts/install_skill.py`` (онбординг-триада)."""
    return _render(
        _INSTALL_SKILL_TEMPLATE,
        {"{{slug}}": slug, "{{slug_repr}}": repr(slug)},
    )


def render_self_check_py(*, slug: str) -> str:
    """Сгенерировать ``scripts/self_check.py`` (pass/warn/fail + --strict)."""
    return _render(_SELF_CHECK_TEMPLATE, {"{{slug}}": slug})


def render_smoke_test_py(*, slug: str) -> str:
    """Сгенерировать ``scripts/smoke_test.py``."""
    return _render(_SMOKE_TEST_TEMPLATE, {"{{slug}}": slug})


# ---------------------------------------------------------------------------
#  pyproject.toml + tooling-CLI скелет (на базе clikit)
# ---------------------------------------------------------------------------
def render_pyproject(*, slug: str, with_cli: bool = True, version: str = DEFAULT_VERSION) -> str:
    """Сгенерировать ``pyproject.toml`` с консольным entry-point для CLI навыка.

    Зеркалит ``clikit.scaffold.render_pyproject``: ``[project.scripts]`` указывает
    ``<slug> = "<pkg>.cli:app"``; в зависимостях — ``clikit`` + ``typer`` (если CLI).
    """
    pkg = module_name(slug)
    deps = '    "clikit",\n    "typer>=0.15",\n' if with_cli else ""
    scripts_block = ""
    wheel_block = ""
    if with_cli:
        scripts_block = f'\n[project.scripts]\n"{slug}" = "{pkg}.cli:app"\n'
        wheel_block = f'\n[tool.hatch.build.targets.wheel]\npackages = ["{pkg}"]\n'
    return (
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[project]\n"
        f'name = "{slug}"\n'
        f'version = "{version}"\n'
        f'description = "CLI навыка {slug} (на базе clikit)."\n'
        'readme = "SKILL.md"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = [\n"
        f"{deps}"
        "]\n"
        f"{scripts_block}"
        f"{wheel_block}"
        "\n"
        "[tool.hatch.metadata]\n"
        "allow-direct-references = true\n"
    )


def render_cli_init(*, slug: str) -> str:
    """Сгенерировать ``<pkg>/__init__.py``."""
    return f'"""{slug} CLI package."""\n\n__version__ = "{DEFAULT_VERSION}"\n'


_CLI_MODULE_TEMPLATE = '''"""{{slug}} — CLI навыка на базе clikit (скелет scaffold-команды).

Замени пример-команды реальной бизнес-логикой навыка. clikit даёт единый
root-app (`build_root_app`) с `--json`/`--profile`, декораторы `@command` /
`@async_command` и `HttpClient` (rest-token) для походов в API.
"""
from __future__ import annotations

import typer

from clikit.command_kit import async_command, build_root_app, command
from clikit.errors import ValidationError
from clikit.output import emit_data

__version__ = "{{version}}"

# brand = имя пакета: задаёт env-ключи {{brand_upper}}_OUTPUT / {{brand_upper}}_PROFILE.
app = build_root_app({{pkg_repr}}, version=__version__)


@app.command(name="ping")
@command
def ping(name: str = typer.Argument("мир")) -> None:
    """Пример sync-команды: печатает приветствие (text|json)."""
    if not name.strip():
        raise ValidationError("имя не может быть пустым")
    emit_data({"hello": name}, text_renderer=lambda d: print(d["hello"]))


@app.command(name="fetch")
@async_command
async def fetch(path: str = typer.Argument("/health")) -> None:
    """Пример async-команды: GET к API через HttpClient.

    BASE_URL и токен подставь из своего config/session-слоя.
    """
    from clikit.transport import HttpClient

    client = HttpClient(base_url="https://api.example.com")
    try:
        data = await client.get_json(path)
    finally:
        await client.aclose()
    emit_data(data)


if __name__ == "__main__":
    app()
'''


def render_tooling_cli_module(*, slug: str, version: str = DEFAULT_VERSION) -> str:
    """Сгенерировать ``<pkg>/cli.py`` — встроенный CLI-скелет на clikit.

    Зеркалит ``clikit.scaffold.render_cli_module`` (rest-token). clikit не
    зависимость клиента, поэтому скелет встроен текстом; clikit подтянется при
    установке навыка (объявлен в ``pyproject.toml``).
    """
    pkg = module_name(slug)
    return _render(
        _CLI_MODULE_TEMPLATE,
        {
            "{{slug}}": slug,
            "{{version}}": version,
            "{{pkg_repr}}": repr(pkg),
            "{{brand_upper}}": pkg.upper(),
        },
    )


__all__ = [
    "DEFAULT_VERSION",
    "SKILL_KINDS",
    "module_name",
    "render_cli_init",
    "render_install_skill_py",
    "render_pyproject",
    "render_readme",
    "render_references_stub",
    "render_self_check_py",
    "render_skill_md",
    "render_skill_meta_toml",
    "render_smoke_test_py",
    "render_tooling_cli_module",
]
