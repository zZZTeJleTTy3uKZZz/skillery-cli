"""``skills-hub new <slug> --kind prompt|comprehensive|tooling`` — scaffold навыка.

Генерит ПАПКУ НАВЫКА по типу. Always-on (работает без логина — это
локальная генерация, сети не нужно):

- ``prompt`` — ``SKILL.md`` (frontmatter + router + 5 секций AK-канон) + ``README.md``.
- ``comprehensive`` — то же + ``references/`` (заглушки) + кейс-секции в ``SKILL.md``.
- ``tooling`` — то же + ``_skill_meta.toml`` (схема: kind, ``[[cli]]``/``[[mcp]]``, ``runtime_dependencies``) + ``--with-cli`` встроенный CLI-скелет на clikit
  (``<pkg>/cli.py`` + ``pyproject.toml`` с entry-point).

Онбординг-триада (``scripts/install_skill.py`` + ``scripts/self_check.py`` +
``SKILL.md``) кладётся в КАЖДЫЙ навык — навык переносим и самопроверяем сразу.

Шаблоны — чистые рендер-функции в ``skillery_cli.templates`` (тестируемы
отдельно). Этот модуль только материализует их на диск + валидирует результат.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from skillery_cli import templates as tpl
from skillery_cli.output import emit_data, emit_error, emit_message

# Минимальный контракт публикации (зеркало skill_emitter.validate_skill):
# SKILL.md с frontmatter + обязательные ключи name/description/version.


def scaffold_skill(
    *,
    slug: str,
    kind: str,
    target_dir: Path | str,
    description: str | None = None,
    with_cli: bool = False,
    with_mcp: bool = False,
    force: bool = False,
) -> Path:
    """Сгенерировать папку навыка ``<target_dir>/<slug>`` по типу ``kind``.

    Возвращает путь к созданной папке. ``force=False`` и существующая непустая
    папка → ``FileExistsError``. ``description`` по умолчанию — заглушка с
    билингв-подсказкой (автор обязан заменить).
    """
    if kind not in tpl.SKILL_KINDS:
        raise ValueError(
            f"неизвестный --kind {kind!r}; допустимо: {', '.join(tpl.SKILL_KINDS)}"
        )
    if not slug or not slug.strip():
        raise ValueError("slug не может быть пустым")

    desc = description or (
        f"TODO: опиши навык {slug} (WHAT + WHEN). "
        "EN triggers: ...; RU triggers: ..."
    )

    skill_dir = Path(target_dir) / slug
    if skill_dir.exists() and any(skill_dir.iterdir()) and not force:
        raise FileExistsError(
            f"папка навыка уже существует и непуста: {skill_dir} (используй --force)"
        )
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)

    # 1) SKILL.md — всегда (часть онбординг-триады)
    (skill_dir / "SKILL.md").write_text(
        tpl.render_skill_md(
            slug=slug,
            kind=kind,
            description=desc,
            with_cli=with_cli,
            with_mcp=with_mcp,
        ),
        encoding="utf-8",
    )
    # 2) README.md — всегда
    (skill_dir / "README.md").write_text(
        tpl.render_readme(slug=slug, kind=kind, description=desc), encoding="utf-8"
    )
    # 3) онбординг-триада скриптов — в КАЖДЫЙ навык
    (skill_dir / "scripts" / "install_skill.py").write_text(
        tpl.render_install_skill_py(slug=slug), encoding="utf-8"
    )
    (skill_dir / "scripts" / "self_check.py").write_text(
        tpl.render_self_check_py(slug=slug), encoding="utf-8"
    )

    # 4) comprehensive — references/ заглушки
    if kind == "comprehensive":
        refs = skill_dir / "references"
        refs.mkdir(parents=True, exist_ok=True)
        for name in ("overview.md", "cases.md"):
            (refs / name).write_text(
                tpl.render_references_stub(slug=slug, name=name), encoding="utf-8"
            )

    # 5) tooling — манифест + smoke + (опц.) CLI-скелет
    if kind == "tooling":
        (skill_dir / "_skill_meta.toml").write_text(
            tpl.render_skill_meta_toml(
                slug=slug, description=desc, with_cli=with_cli, with_mcp=with_mcp
            ),
            encoding="utf-8",
        )
        (skill_dir / "scripts" / "smoke_test.py").write_text(
            tpl.render_smoke_test_py(slug=slug), encoding="utf-8"
        )
        if with_cli:
            pkg_dir = skill_dir / tpl.module_name(slug)
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "__init__.py").write_text(
                tpl.render_cli_init(slug=slug), encoding="utf-8"
            )
            (pkg_dir / "cli.py").write_text(
                tpl.render_tooling_cli_module(slug=slug), encoding="utf-8"
            )
            (skill_dir / "pyproject.toml").write_text(
                tpl.render_pyproject(slug=slug, with_cli=True), encoding="utf-8"
            )

    return skill_dir


def validate_scaffolded_skill(skill_dir: Path | str) -> list[str]:
    """Проверить, что папка навыка пригодна для публикации (минимальный контракт).

    Возвращает список ошибок (пустой = валиден). Зеркало
    ``skill_emitter.validate_skill``: каталог существует, есть ``SKILL.md`` с
    frontmatter-блоком и обязательными ключами ``name``/``version``/
    ``description``. Для tooling дополнительно: ``_skill_meta.toml`` парсится
    (stdlib tomllib) и несёт ``kind="tooling"`` с хотя бы одной ``[[cli]]``/
    ``[[mcp]]`` секцией (зеркало backend-валидации публикации tooling).
    """
    import tomllib

    errors: list[str] = []
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        return [f"Каталог навыка не найден: {skill_dir}"]

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        errors.append("Нет SKILL.md в корне навыка.")
    else:
        fm = _frontmatter_keys(skill_md.read_text(encoding="utf-8"))
        if fm is None:
            errors.append("SKILL.md без frontmatter-блока (--- … ---).")
        else:
            for key in ("name", "version", "description"):
                if not fm.get(key):
                    errors.append(f"В frontmatter нет обязательного ключа {key}.")

    meta = skill_dir / "_skill_meta.toml"
    if meta.is_file():
        try:
            data = tomllib.loads(meta.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"_skill_meta.toml не парсится: {exc}")
        else:
            if not data.get("description"):
                errors.append("_skill_meta.toml: нет обязательного 'description'.")
            if not data.get("version"):
                errors.append("_skill_meta.toml: нет обязательного 'version'.")
            if data.get("kind") == "tooling" and not (
                data.get("cli") or data.get("mcp")
            ):
                errors.append(
                    "tooling-навык должен нести CLI или MCP "
                    "([[cli]]/[[mcp]] в _skill_meta.toml)."
                )
    return errors


def _unwrap(value: Any, fallback: Any) -> Any:
    """Снять typer-sentinel (OptionInfo/ArgumentInfo) к реальному значению.

    При прямом вызове ``cmd_new`` из кода/тестов невыставленные параметры
    приходят как ``typer.models.OptionInfo`` (а не их ``default``). Берём
    ``.default`` если он осмысленный, иначе ``fallback``.
    """
    if isinstance(value, (typer.models.OptionInfo, typer.models.ArgumentInfo)):
        default = getattr(value, "default", None)
        if isinstance(default, (typer.models.OptionInfo, typer.models.ArgumentInfo)):
            return fallback
        return default if default is not ... else fallback
    return value


def _frontmatter_keys(text: str) -> dict[str, str] | None:
    """Достать верхнеуровневые ключи frontmatter (только наличие, без deep-parse)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    keys: dict[str, str] = {}
    for i in range(1, len(lines)):
        raw = lines[i]
        if raw.strip() == "---":
            return keys
        if not raw.strip() or raw.lstrip() != raw:  # пустая или с отступом
            continue
        if raw.lstrip().startswith(("#", "-")):
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        keys[key.strip()] = value.strip()
    return None  # закрывающего --- не нашли


def cmd_new(
    slug: str = typer.Argument(..., help="slug нового навыка (имя папки)"),
    kind: str = typer.Option(
        "prompt",
        "--kind",
        "-k",
        help="Тип навыка: prompt | comprehensive | tooling.",
    ),
    with_cli: bool = typer.Option(
        False, "--with-cli", help="(tooling) Сгенерировать CLI-скелет на clikit."
    ),
    with_mcp: bool = typer.Option(
        False, "--with-mcp", help="(tooling) Объявить MCP-сервер в манифесте."
    ),
    directory: Path = typer.Option(
        None,
        "--dir",
        help="Каталог-родитель для папки навыка (default: cwd).",
    ),
    description: str = typer.Option(
        None, "--description", "-d", help="Описание навыка для frontmatter/манифеста."
    ),
    force: bool = typer.Option(
        False, "--force", help="Перезаписать существующую папку навыка."
    ),
) -> None:
    """Создать скелет нового навыка по типу (prompt/comprehensive/tooling).

    Кладёт ``SKILL.md`` (router + 5 секций AK-канон) + онбординг-триаду в каждый
    тип; comprehensive добавляет ``references/``; tooling — ``_skill_meta.toml``
    (схема) и, с ``--with-cli``, CLI-скелет на clikit. После генерации
    валидирует навык под минимальный контракт публикации.
    """
    # Прямой вызов (тесты) минует typer-резолвинг → дефолты приходят как
    # OptionInfo. Снимаем их к реальным значениям (паттерн совместимости).
    kind = _unwrap(kind, "prompt")
    with_cli = _unwrap(with_cli, False)
    with_mcp = _unwrap(with_mcp, False)
    directory = _unwrap(directory, None)
    description = _unwrap(description, None)
    force = _unwrap(force, False)

    if kind not in tpl.SKILL_KINDS:
        emit_error(
            "VALIDATION",
            f"неизвестный --kind {kind!r}; допустимо: {', '.join(tpl.SKILL_KINDS)}",
        )
        raise typer.Exit(2)

    parent = (directory or Path.cwd()).resolve()
    if not parent.is_dir():
        emit_error("VALIDATION", f"Каталог-родитель не найден: {parent}")
        raise typer.Exit(2)

    # tooling без cli/mcp → подсказка (но навык всё равно создаём).
    if kind == "tooling" and not (with_cli or with_mcp):
        emit_message(
            "tooling-навык без --with-cli/--with-mcp: манифест получит kind=tooling, "
            "но без CLI/MCP backend отвергнет публикацию. Добавь --with-cli или --with-mcp "
            "(либо допиши [[cli]]/[[mcp]] в _skill_meta.toml вручную).",
            level="warn",
        )

    try:
        skill_dir = scaffold_skill(
            slug=slug,
            kind=kind,
            target_dir=parent,
            description=description,
            with_cli=with_cli,
            with_mcp=with_mcp,
            force=force,
        )
    except FileExistsError as exc:
        emit_error("CONFLICT", str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        emit_error("VALIDATION", str(exc))
        raise typer.Exit(2) from exc

    errors = validate_scaffolded_skill(skill_dir)

    payload: dict[str, Any] = {
        "event": "scaffolded",
        "slug": slug,
        "kind": kind,
        "with_cli": with_cli,
        "with_mcp": with_mcp,
        "path": str(skill_dir),
        "valid": not errors,
        "validation_errors": errors,
    }

    def _render(p: dict[str, Any]) -> None:
        emit_message(f"Навык создан: {p['path']} (kind={p['kind']})")
        if p["kind"] == "tooling" and p["with_cli"]:
            emit_message("CLI-скелет на clikit готов — см. pyproject.toml entry-point.")
        if p["validation_errors"]:
            emit_message(
                "Внимание: навык не проходит контракт публикации:", level="warn"
            )
            for err in p["validation_errors"]:
                emit_message(f"  - {err}", level="warn")
        else:
            emit_message("Контракт публикации: OK.")
        emit_message(
            "Дальше: установи в агента → "
            f"python {Path(p['path']) / 'scripts' / 'install_skill.py'} --agent claude-code"
        )

    emit_data(payload, text_renderer=_render)


def register(app: typer.Typer) -> None:
    """Регистрирует always-on команду ``new`` (scaffold навыка)."""
    app.command(name="new")(cmd_new)
