"""wave-B: «hub-admin»/«skill-creator» выводятся из прав, а не из флагов.

После under-волны A права идут от глобальных memberships и приходят в JWT
обычными permission-ключами. CLI больше НЕ хранит флаги is_hub_admin/
is_skill_creator — признак роли вычисляется из `permissions`.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from skills_hub_cli.config import ClientConfig, populate_from_jwt


def _make_jwt(claims: dict) -> str:
    """Собрать unsigned JWT (CLI читает payload без верификации)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    )
    return f"{header}.{payload}.sig"


# ---------------------- is_hub_admin / is_skill_creator from permissions ----------------------
def test_is_hub_admin_true_when_permission_present() -> None:
    cfg = ClientConfig(permissions=["hub.admin", "skill.read"])
    assert cfg.is_hub_admin() is True


def test_is_hub_admin_false_without_permission() -> None:
    cfg = ClientConfig(permissions=["skill.read", "skill.install"])
    assert cfg.is_hub_admin() is False


def test_is_skill_creator_true_when_publish_present() -> None:
    cfg = ClientConfig(permissions=["skill.read", "skill.publish"])
    assert cfg.is_skill_creator() is True


def test_is_skill_creator_false_without_publish() -> None:
    cfg = ClientConfig(permissions=["skill.read"])
    assert cfg.is_skill_creator() is False


# ---------------------- dataclass has no stored flag fields ----------------------
def test_config_has_no_flag_fields() -> None:
    cfg = ClientConfig(permissions=["hub.admin"])
    assert not hasattr(cfg, "_is_hub_admin_field")  # sanity
    # Поля-флаги удалены: dataclass их не объявляет.
    field_names = {f.name for f in cfg.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert "is_hub_admin" not in field_names
    assert "is_skill_creator" not in field_names


# ---------------------- populate_from_jwt НЕ читает права из токена (JWT-slim) ----------------------
def test_populate_from_jwt_ignores_permissions_claim() -> None:
    """JWT-slim: populate_from_jwt заполняет role_id/company_id/exp, но НЕ права.

    Источник эффективных прав — ``GET /me/permissions`` (БД-авторитетно), их
    проставляет ``_common.hydrate_session_permissions`` в login-флоу. Даже если
    в (legacy) токене есть claim ``permissions``, в cfg он НЕ попадает —
    единый источник правды один.
    """
    cfg = ClientConfig()
    token = _make_jwt(
        {
            "permissions": ["hub.admin", "skill.publish", "skill.read"],
            "role_id": "1",
            "company_id": "2",
            "exp": 9999999999,
        }
    )
    populate_from_jwt(cfg, token)
    assert cfg.permissions == []  # права из токена НЕ берутся
    assert cfg.role_id == "1"
    assert cfg.company_id == "2"


# ---------------------- save/load round-trip без флагов ----------------------
def test_save_load_does_not_persist_flags(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="admin@hub",
        permissions=["hub.admin", "skill.publish"],
    )
    cfg.save(cfg_path)

    raw = cfg_path.read_text(encoding="utf-8")
    assert "is_hub_admin" not in raw
    assert "is_skill_creator" not in raw

    loaded = ClientConfig.load(cfg_path)
    assert loaded.permissions == ["hub.admin", "skill.publish"]
    assert loaded.is_hub_admin() is True
    assert loaded.is_skill_creator() is True


def test_load_ignores_legacy_flag_keys(tmp_path: Path) -> None:
    """Старый config.toml с is_hub_admin=true не должен ломать load (ключ игнорится)."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        'base_url = "http://localhost:8000"\n'
        'permissions = ["skill.read"]\n'
        "is_hub_admin = true\n"
        "is_skill_creator = true\n",
        encoding="utf-8",
    )
    loaded = ClientConfig.load(cfg_path)
    # Legacy-флаги проигнорированы; роль теперь только от прав.
    assert loaded.permissions == ["skill.read"]
    assert loaded.is_hub_admin() is False
    assert loaded.is_skill_creator() is False
