"""cli-kits W6 — каркас команд переведён на ``clikit.command_kit``.

Инварианты миграции (поведение/набор команд НЕ меняются):

1. Root-app строится через ``command_kit.build_root_app`` — но callback и
   набор команд остаются ИСТОРИЧЕСКИМИ: авто-подкоманда ``version`` снята
   (версия только глобальным флагом ``--version``), флаг ``--text``/``--plain``
   из build_root_app не торчит наружу (контракт вывода: дефолт text,
   ``--json`` переключает — как до W6).
2. Одиночные permission-гейты в ``build_app`` и ``commands/*`` теперь идут
   через ``command_kit.gated`` — регистрация/скрытие подкоманд идентичны
   прежним ``if cfg.has_permission(...)``.

Подробные RBAC-гейты по доменам покрыты в ``test_dcli_commands.py`` — здесь
только W6-специфичные инварианты каркаса.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from skills_hub_cli.config import ClientConfig


def _build(monkeypatch: pytest.MonkeyPatch, perms: list[str] | None):  # noqa: ANN202
    if perms is None:
        cfg = ClientConfig(base_url="http://localhost:8000")
    else:
        cfg = ClientConfig(
            base_url="http://localhost:8000",
            user_email="u@example.com",
            permissions=perms,
            company_id="1",
        )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    return build_app()


def test_no_version_subcommand_only_global_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_root_app регистрирует подкоманду ``version`` — она снята: версия
    печатается ТОЛЬКО глобальным флагом ``--version`` (исторический набор)."""
    app = _build(monkeypatch, None)
    names = [c.name for c in app.registered_commands]
    assert "version" not in names
    # глобальный флаг --version по-прежнему работает
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0


def test_no_text_plain_flag_output_contract_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--text``/``--plain`` из build_root_app наружу НЕ торчат (исторический
    контракт: дефолт text, ``--json`` переключает)."""
    app = _build(monkeypatch, None)
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--text" not in result.stdout
    assert "--plain" not in result.stdout
    # глобальные флаги исторического контракта на месте
    assert "--json" in result.stdout
    assert "--version" in result.stdout
    assert "--profile" in result.stdout


def test_always_on_commands_present_without_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Always-on набор (без логина) построен через каркас command_kit."""
    app = _build(monkeypatch, None)
    names = [c.name for c in app.registered_commands]
    for cmd in ("login", "status", "logout", "whoami", "install", "config"):
        assert cmd in names, cmd


def test_gated_single_commands_follow_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gated()-команды появляются ровно при наличии права и исчезают без него."""
    # publish — gated на skill.publish; update — gated на skill.install.
    app = _build(monkeypatch, ["skill.publish", "skill.install", "skill.read"])
    names = [c.name for c in app.registered_commands]
    assert "publish" in names
    assert "update" in names

    app2 = _build(monkeypatch, ["skill.read"])
    names2 = [c.name for c in app2.registered_commands]
    assert "publish" not in names2
    assert "update" not in names2


def test_admin_subapp_gated_subcommands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """admin sub-app: подкоманды зарегистрированы через gated по предикатам."""
    app = _build(monkeypatch, ["hub.admin"])
    admin_group = next(
        (g for g in app.registered_groups if g.name == "admin"), None
    )
    assert admin_group is not None
    sub = [c.name for c in admin_group.typer_instance.registered_commands]
    # hub.admin → sync-skill + invite (company-create требует hub.company_create)
    assert "sync-skill" in sub
    assert "invite" in sub
