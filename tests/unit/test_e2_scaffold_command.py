"""E2 — тесты команды ``skillery new <slug> --kind ...`` (scaffold).

Команда генерит ПАПКУ НАВЫКА по типу (prompt/comprehensive/tooling) в tmp_path.
Проверяем структуру каждого типа, наличие онбординг-триады в КАЖДОМ навыке,
валидность ``_skill_meta.toml`` под E6-схему, импортируемость tooling-CLI
скелета и регистрацию команды always-on в build_app.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import typer

from skillery_cli import output as output_module
from skillery_cli.commands import scaffold as scaffold_mod
from skillery_cli.config import ClientConfig


def _text_mode() -> None:
    output_module._mode = "text"


# ======================================================
#  Регистрация — always-on (без логина)
# ======================================================
def test_new_command_registered_always_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # даже без логина команда `new` должна быть зарегистрирована
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "new" in names


# ======================================================
#  scaffold_skill — публичная функция записи на диск
# ======================================================
def test_scaffold_prompt_layout(tmp_path: Path) -> None:
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="my-prompt", kind="prompt", target_dir=tmp_path, description="Делает X"
    )
    assert skill_dir == tmp_path / "my-prompt"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "README.md").is_file()
    # онбординг-триада есть и в prompt (install_skill + self_check + SKILL.md)
    assert (skill_dir / "scripts" / "install_skill.py").is_file()
    assert (skill_dir / "scripts" / "self_check.py").is_file()
    # prompt — без tooling-артефактов
    assert not (skill_dir / "_skill_meta.toml").exists()
    assert not (skill_dir / "scripts" / "smoke_test.py").exists()


def test_scaffold_comprehensive_layout(tmp_path: Path) -> None:
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="big-skill", kind="comprehensive", target_dir=tmp_path, description="d"
    )
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "README.md").is_file()
    # references/ с заглушкой
    refs = skill_dir / "references"
    assert refs.is_dir()
    assert any(refs.iterdir()), "references/ пуст"


def test_scaffold_tooling_layout_and_triad(tmp_path: Path) -> None:
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="my-tool",
        kind="tooling",
        target_dir=tmp_path,
        description="d",
        with_cli=True,
        with_mcp=False,
    )
    # tooling-манифест
    meta = skill_dir / "_skill_meta.toml"
    assert meta.is_file()
    # онбординг-триада
    assert (skill_dir / "scripts" / "install_skill.py").is_file()
    assert (skill_dir / "scripts" / "self_check.py").is_file()
    assert (skill_dir / "scripts" / "smoke_test.py").is_file()
    # SKILL.md есть всегда
    assert (skill_dir / "SKILL.md").is_file()


def test_scaffold_triad_present_in_every_kind(tmp_path: Path) -> None:
    """Онбординг-триада (install_skill + self_check) обязана быть в КАЖДОМ навыке."""
    _text_mode()
    for kind in ("prompt", "comprehensive", "tooling"):
        d = scaffold_mod.scaffold_skill(
            slug=f"k-{kind}", kind=kind, target_dir=tmp_path, description="d"
        )
        assert (d / "scripts" / "install_skill.py").is_file(), kind
        assert (d / "scripts" / "self_check.py").is_file(), kind
        # SKILL.md — третий элемент триады
        assert (d / "SKILL.md").is_file(), kind


# ======================================================
#  _skill_meta.toml валиден под E6-схему
# ======================================================
def test_scaffold_tooling_meta_toml_valid_e6(tmp_path: Path) -> None:
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="bx",
        kind="tooling",
        target_dir=tmp_path,
        description="Bitrix wrapper",
        with_cli=True,
        with_mcp=True,
    )
    data = tomllib.loads((skill_dir / "_skill_meta.toml").read_text(encoding="utf-8"))
    assert data["kind"] == "tooling"
    assert len(data["cli"]) == 1
    assert len(data["mcp"]) == 1
    assert data["cli"][0]["command_name"] == "bx"


# ======================================================
#  tooling-CLI скелет импортируется (через clikit/встроенный шаблон)
# ======================================================
def test_scaffold_tooling_cli_module_written(tmp_path: Path) -> None:
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="my-tool",
        kind="tooling",
        target_dir=tmp_path,
        description="d",
        with_cli=True,
    )
    pkg = skill_dir / "my_tool"
    assert (pkg / "__init__.py").is_file()
    assert (pkg / "cli.py").is_file()
    assert (skill_dir / "pyproject.toml").is_file()


def test_scaffold_tooling_cli_compiles(tmp_path: Path) -> None:
    """Сгенерённый cli.py компилируется как валидный python."""
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="my-tool",
        kind="tooling",
        target_dir=tmp_path,
        description="d",
        with_cli=True,
    )
    src = (skill_dir / "my_tool" / "cli.py").read_text(encoding="utf-8")
    compile(src, "cli.py", "exec")


def test_scaffold_tooling_cli_importable_when_clikit_present(tmp_path: Path) -> None:
    """Если clikit установлен — сгенерённый CLI реально импортируется."""
    pytest.importorskip("clikit", reason="clikit не установлен в окружении клиента")
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="my-tool",
        kind="tooling",
        target_dir=tmp_path,
        description="d",
        with_cli=True,
    )
    # импорт во вложенном процессе с sys.path = папка навыка
    code = "import sys; sys.path.insert(0, %r); import my_tool.cli as c; assert c.app" % str(
        skill_dir
    )
    res = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stderr


# ======================================================
#  Валидация публикации сгенерённого навыка
# ======================================================
def test_scaffolded_prompt_passes_validation(tmp_path: Path) -> None:
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="ok-skill", kind="prompt", target_dir=tmp_path, description="d"
    )
    errors = scaffold_mod.validate_scaffolded_skill(skill_dir)
    assert errors == [], errors


def test_scaffolded_tooling_passes_validation(tmp_path: Path) -> None:
    _text_mode()
    skill_dir = scaffold_mod.scaffold_skill(
        slug="ok-tool",
        kind="tooling",
        target_dir=tmp_path,
        description="d",
        with_cli=True,
    )
    errors = scaffold_mod.validate_scaffolded_skill(skill_dir)
    assert errors == [], errors


def test_validate_detects_missing_skill_md(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    errors = scaffold_mod.validate_scaffolded_skill(tmp_path / "empty")
    assert errors
    assert any("SKILL.md" in e for e in errors)


# ======================================================
#  cmd_new — поведение команды
# ======================================================
def test_cmd_new_rejects_invalid_kind(tmp_path: Path) -> None:
    _text_mode()
    with pytest.raises(typer.Exit) as exc:
        scaffold_mod.cmd_new(slug="x", kind="weird", directory=tmp_path)
    assert exc.value.exit_code != 0


def test_cmd_new_rejects_existing_without_force(tmp_path: Path) -> None:
    _text_mode()
    (tmp_path / "dup").mkdir()
    (tmp_path / "dup" / "SKILL.md").write_text("x", encoding="utf-8")
    with pytest.raises(typer.Exit) as exc:
        scaffold_mod.cmd_new(slug="dup", kind="prompt", directory=tmp_path)
    assert exc.value.exit_code != 0


def test_cmd_new_creates_prompt_skill(tmp_path: Path) -> None:
    _text_mode()
    scaffold_mod.cmd_new(slug="fresh", kind="prompt", directory=tmp_path)
    assert (tmp_path / "fresh" / "SKILL.md").is_file()


def test_cmd_new_tooling_without_cli_or_mcp_hints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """tooling без --with-cli/--with-mcp → подсказка пользователю."""
    _text_mode()
    captured: dict[str, bool] = {}

    real_emit = output_module.emit_message

    def _spy(text: str, *, level: str = "info", **extra: object) -> None:
        if level in ("warn", "error"):
            captured["hinted"] = True
        real_emit(text, level=level, **extra)

    monkeypatch.setattr(scaffold_mod, "emit_message", _spy, raising=False)
    scaffold_mod.cmd_new(slug="bare-tool", kind="tooling", directory=tmp_path)
    # навык всё равно создан, но была подсказка
    assert (tmp_path / "bare-tool" / "SKILL.md").is_file()
    assert captured.get("hinted") is True


def test_cmd_new_json_mode_emits_structured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_module._mode = "json"
    try:
        scaffold_mod.cmd_new(slug="jtool", kind="tooling", directory=tmp_path, with_cli=True)
        out = capsys.readouterr().out
    finally:
        output_module._mode = "text"
    import json

    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["slug"] == "jtool"
    assert payload["kind"] == "tooling"
    assert "path" in payload
