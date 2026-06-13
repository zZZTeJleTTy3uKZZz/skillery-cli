"""E2 — тесты чистых рендер-функций шаблонов навыка (``templates`` пакет).

Рендереры — чистые строки с ``{{token}}``-подстановкой (как ``skill_emitter``
из reverse-factory): на вход slug/kind/флаги, на выходе валидный текст файла.
Тестируются изолированно, без записи на диск и без CLI.
"""
from __future__ import annotations

import tomllib

import pytest

from skills_hub_cli import templates as tpl


# ======================================================
#  module_name / slug нормализация
# ======================================================
def test_module_name_basic() -> None:
    assert tpl.module_name("my-skill") == "my_skill"
    assert tpl.module_name("my.skill.tool") == "my_skill_tool"


def test_module_name_leading_digit_prefixed() -> None:
    # python-идентификатор не может начинаться с цифры
    assert tpl.module_name("2fa-cli")[0] == "_"


def test_module_name_empty_fallback() -> None:
    assert tpl.module_name("---") == "skill"


# ======================================================
#  render_skill_md — frontmatter + router + 5 секций (AK-канон)
# ======================================================
def test_render_skill_md_has_frontmatter_keys() -> None:
    md = tpl.render_skill_md(slug="my-skill", kind="prompt", description="Делает X")
    assert md.startswith("---\n")
    # обязательные ключи frontmatter
    assert "\nname: my-skill\n" in md
    assert "\nversion:" in md
    assert "description:" in md
    # description в двойных кавычках (AK-канон)
    assert 'description: "Делает X"' in md


def test_render_skill_md_double_quote_escapes_quotes() -> None:
    md = tpl.render_skill_md(
        slug="s", kind="prompt", description='со "кавычками" внутри'
    )
    assert r'description: "со \"кавычками\" внутри"' in md


def test_render_skill_md_router_and_five_sections() -> None:
    md = tpl.render_skill_md(slug="my-skill", kind="prompt", description="d")
    # router-таблица
    assert "Route by request type" in md or "Маршрутизация" in md
    # 5 канон-секций
    for section in (
        "When activated",
        "Instructions",
        "Examples",
        "Rules",
    ):
        assert section in md, f"нет секции {section}"
    # «Respond in the user's language» — AK-канон тела
    assert "Respond in the user's language." in md


def test_render_skill_md_frontmatter_carries_kind() -> None:
    # kind во frontmatter — нужен для from-git публикации без _skill_meta.toml
    for kind in ("prompt", "comprehensive", "tooling"):
        md = tpl.render_skill_md(slug="s", kind=kind, description="d")
        assert f"\nkind: {kind}\n" in md


def test_render_skill_md_frontmatter_parses_via_backend() -> None:
    backend = pytest.importorskip(
        "skills_hub_backend.domain.skill.manifest",
        reason="backend не в окружении клиента",
    )
    md = tpl.render_skill_md(slug="s", kind="tooling", description="d")
    manifest = backend.SkillManifest.parse_skill_md(md)
    assert manifest.kind is backend.SkillKind.tooling


def test_render_skill_md_comprehensive_mentions_references_and_cases() -> None:
    md = tpl.render_skill_md(slug="big", kind="comprehensive", description="d")
    # comprehensive несёт references/ и кейсы
    assert "references/" in md
    low = md.lower()
    assert "кейс" in low or "пример" in low


def test_render_skill_md_tooling_mentions_scripts_triad() -> None:
    md = tpl.render_skill_md(
        slug="tool", kind="tooling", description="d", with_cli=True, with_mcp=False
    )
    # tooling несёт онбординг-триаду в теле
    assert "scripts/install_skill.py" in md
    assert "scripts/self_check.py" in md


def test_render_skill_md_no_unrendered_tokens() -> None:
    for kind in ("prompt", "comprehensive", "tooling"):
        md = tpl.render_skill_md(slug="x-y", kind=kind, description="d")
        assert "{{" not in md and "}}" not in md, f"остались токены в {kind}"


# ======================================================
#  render_skill_meta_toml — E6-схема (kind/[[cli]]/[[mcp]]/runtime_deps)
# ======================================================
def test_render_meta_toml_parses_and_is_tooling() -> None:
    text = tpl.render_skill_meta_toml(
        slug="tool", description="d", with_cli=True, with_mcp=True
    )
    data = tomllib.loads(text)
    assert data["kind"] == "tooling"
    assert data["description"] == "d"
    assert "version" in data


def test_render_meta_toml_with_cli_section() -> None:
    text = tpl.render_skill_meta_toml(
        slug="my-tool", description="d", with_cli=True, with_mcp=False
    )
    data = tomllib.loads(text)
    assert isinstance(data["cli"], list) and len(data["cli"]) == 1
    entry = data["cli"][0]
    assert entry["command_name"] == "my-tool"
    # entrypoint указывает на пакет навыка
    assert entry["entrypoint"].startswith(tpl.module_name("my-tool"))
    # без mcp — секции нет
    assert "mcp" not in data or data["mcp"] == []


def test_render_meta_toml_with_mcp_section() -> None:
    text = tpl.render_skill_meta_toml(
        slug="srv", description="d", with_cli=False, with_mcp=True
    )
    data = tomllib.loads(text)
    assert isinstance(data["mcp"], list) and len(data["mcp"]) == 1
    entry = data["mcp"][0]
    assert entry["server_name"]
    assert entry["transport"] in ("stdio", "sse", "http")


def test_render_meta_toml_runtime_dependencies_block_present() -> None:
    text = tpl.render_skill_meta_toml(
        slug="srv", description="d", with_cli=True, with_mcp=False
    )
    data = tomllib.loads(text)
    # секция присутствует (может быть пустой/закомментированной как пример)
    assert "runtime_dependencies" in data
    assert isinstance(data["runtime_dependencies"], list)


def test_render_meta_toml_validates_against_backend_schema() -> None:
    """Сгенерённый TOML валиден под E6-парсер бэкенда, если тот доступен."""
    backend = pytest.importorskip(
        "skills_hub_backend.domain.skill.manifest",
        reason="backend не в окружении клиента — schema-проверка через tomllib",
    )
    text = tpl.render_skill_meta_toml(
        slug="bx", description="Bitrix wrapper", with_cli=True, with_mcp=True
    )
    manifest = backend.SkillManifest.parse_skill_meta_toml(text)
    assert manifest.kind is backend.SkillKind.tooling
    assert manifest.has_cli() is True
    assert manifest.has_mcp() is True


# ======================================================
#  Онбординг-триада: install_skill / self_check / smoke_test
# ======================================================
@pytest.mark.parametrize(
    "renderer",
    ["render_install_skill_py", "render_self_check_py", "render_smoke_test_py"],
)
def test_triad_renders_valid_python(renderer: str) -> None:
    text = getattr(tpl, renderer)(slug="my-skill")
    # компилируется как валидный python (нет {{token}})
    assert "{{" not in text and "}}" not in text
    compile(text, f"<{renderer}>", "exec")


def test_install_skill_has_home_template_and_uv_pip_fallback() -> None:
    text = tpl.render_install_skill_py(slug="my-skill")
    # кроссплатформенный {home}-шаблон каталога агента
    assert "{home}" in text
    assert ".claude/skills" in text
    # --with-deps uv→pip fallback
    assert "--with-deps" in text
    assert "uv" in text and "pip" in text


def test_self_check_has_levels_and_strict() -> None:
    text = tpl.render_self_check_py(slug="my-skill")
    # pass/warn/fail уровни + --strict
    assert "--strict" in text
    assert "fail" in text and "warn" in text and "pass" in text


# ======================================================
#  render_pyproject — entry-point для tooling-CLI
# ======================================================
def test_render_pyproject_has_entrypoint() -> None:
    text = tpl.render_pyproject(slug="my-tool", with_cli=True)
    data = tomllib.loads(text)
    assert data["project"]["name"] == "my-tool"
    scripts = data["project"]["scripts"]
    assert "my-tool" in scripts
    pkg = tpl.module_name("my-tool")
    assert scripts["my-tool"].startswith(f"{pkg}.cli:")


def test_render_pyproject_declares_clikit_dep_when_cli() -> None:
    text = tpl.render_pyproject(slug="t", with_cli=True)
    data = tomllib.loads(text)
    assert any("clikit" in d for d in data["project"]["dependencies"])


# ======================================================
#  render_tooling_cli_module — встроенный clikit-скелет
# ======================================================
def test_render_cli_module_uses_clikit_and_compiles() -> None:
    text = tpl.render_tooling_cli_module(slug="my-tool")
    assert "{{" not in text and "}}" not in text
    assert "clikit" in text
    assert "build_root_app" in text
    compile(text, "<cli>", "exec")
