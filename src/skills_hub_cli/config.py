"""Конфигурация клиента: ~/.skills-hub/[profiles/<name>/]config.toml + keyring токены."""
from __future__ import annotations

import base64
import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

KEYRING_SERVICE = "skills-hub-cli"

# Текущий активный профиль (--profile / SKILLS_HUB_PROFILE).
_ACTIVE_PROFILE: str | None = None


def set_active_profile(profile: str | None) -> None:
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = profile or os.environ.get("SKILLS_HUB_PROFILE")


def active_profile() -> str | None:
    return _ACTIVE_PROFILE or os.environ.get("SKILLS_HUB_PROFILE")


def _default_config_dir() -> Path:
    base = Path(
        os.environ.get("SKILLS_HUB_CONFIG_DIR", "~/.skills-hub")
    ).expanduser()
    profile = active_profile()
    if profile:
        return base / "profiles" / profile
    return base


def _default_config_file() -> Path:
    return _default_config_dir() / "config.toml"


def _default_base_url() -> str:
    return os.environ.get("SKILLS_HUB_BASE_URL", "http://localhost:8000")


def _keyring_namespace() -> str:
    profile = active_profile()
    return f"{KEYRING_SERVICE}:{profile}" if profile else KEYRING_SERVICE


# Совместимость со старым API (lazy для тестов)
DEFAULT_CONFIG_DIR = _default_config_dir()
DEFAULT_CONFIG_FILE = _default_config_file()
DEFAULT_BASE_URL = _default_base_url()


@dataclass
class ClientConfig:
    base_url: str = ""
    user_email: str | None = None
    agent: str | None = None
    permissions: list[str] = field(default_factory=list)
    """Permission keys из JWT (используется для модульной регистрации команд)."""
    is_hub_admin: bool = False
    is_skill_creator: bool = False
    company_id: str | None = None
    role_id: str | None = None
    access_expires_at: str | None = None  # iso-формат
    auto_update: bool = True
    """При вызовах list/show/install — тихо подтянуть новые версии установленных skills."""
    auto_update_cooldown_min: int = 60
    """Не чаще раза в N минут."""
    last_auto_update_at: str | None = None
    output_format: str = "text"
    """text | json — формат вывода по умолчанию.
    Можно переопределить через --json флаг или env SKILLS_HUB_OUTPUT=json.
    Для AI агентов рекомендуется json."""
    default_install_scope: str = "global"
    """global | project — куда install ставит skill по умолчанию (без --scope).
    global → ~/.claude/skills/<slug>/  (видны во всех проектах)
    project → <project>/.claude/skills/<slug>/  (только в указанном проекте)
    """
    default_project_dir: str | None = None
    """Дефолтный project root для scope=project. Если null — текущий cwd."""
    web_ui_url: str | None = None
    """https://hub.example — куда CLI открывает браузер для handoff в Web UI.
    None → derive из base_url (api.* → host, localhost:8000 → localhost:3000)."""

    def __post_init__(self) -> None:
        if not self.base_url:
            self.base_url = _default_base_url()

    def effective_web_ui_url(self) -> str:
        """URL Web UI: explicit override > derive из base_url > base_url как fallback."""
        if self.web_ui_url:
            return self.web_ui_url
        base = self.base_url
        # http(s)://api.<host> → http(s)://<host>
        for scheme in ("https://", "http://"):
            if base.startswith(scheme + "api."):
                return scheme + base[len(scheme) + len("api."):]
        # http://localhost:8000 (dev) → http://localhost:3000 (Next.js dev)
        if "localhost:8000" in base:
            return base.replace("localhost:8000", "localhost:3000")
        return base

    @classmethod
    def load(cls, path: Path | None = None) -> ClientConfig:
        actual_path = path or _default_config_file()
        if not actual_path.exists():
            return cls()
        raw = actual_path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(raw)
        return cls(
            base_url=data.get("base_url", _default_base_url()),
            user_email=data.get("user_email"),
            agent=data.get("agent"),
            permissions=list(data.get("permissions", [])),
            is_hub_admin=bool(data.get("is_hub_admin", False)),
            is_skill_creator=bool(data.get("is_skill_creator", False)),
            company_id=data.get("company_id"),
            role_id=data.get("role_id"),
            access_expires_at=data.get("access_expires_at"),
            auto_update=bool(data.get("auto_update", True)),
            auto_update_cooldown_min=int(data.get("auto_update_cooldown_min", 60)),
            last_auto_update_at=data.get("last_auto_update_at"),
            output_format=str(data.get("output_format", "text")),
            default_install_scope=str(data.get("default_install_scope", "global")),
            default_project_dir=data.get("default_project_dir"),
            web_ui_url=data.get("web_ui_url"),
        )

    def save(self, path: Path | None = None) -> None:
        actual_path = path or _default_config_file()
        actual_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, object] = {"base_url": self.base_url}
        if self.user_email:
            data["user_email"] = self.user_email
        if self.agent:
            data["agent"] = self.agent
        if self.permissions:
            data["permissions"] = self.permissions
        if self.is_hub_admin:
            data["is_hub_admin"] = self.is_hub_admin
        if self.is_skill_creator:
            data["is_skill_creator"] = self.is_skill_creator
        if self.company_id:
            data["company_id"] = self.company_id
        if self.role_id:
            data["role_id"] = self.role_id
        if self.access_expires_at:
            data["access_expires_at"] = self.access_expires_at
        data["auto_update"] = self.auto_update
        data["auto_update_cooldown_min"] = self.auto_update_cooldown_min
        if self.last_auto_update_at:
            data["last_auto_update_at"] = self.last_auto_update_at
        data["output_format"] = self.output_format
        data["default_install_scope"] = self.default_install_scope
        if self.default_project_dir:
            data["default_project_dir"] = self.default_project_dir
        if self.web_ui_url:
            data["web_ui_url"] = self.web_ui_url
        actual_path.write_text(tomli_w.dumps(data), encoding="utf-8")

    def has_permission(self, permission_key: str) -> bool:
        if "hub.admin" in self.permissions:
            return True
        return permission_key in self.permissions

    def is_logged_in(self) -> bool:
        return bool(self.user_email and self.permissions)


def decode_jwt_claims(access_token: str) -> dict[str, object]:
    """Декодирует payload JWT БЕЗ верификации подписи.

    Верификация — задача backend; CLI просто читает claims чтобы знать
    свои permissions и не делать запросы которые точно упадут с 403.
    """
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # Padding
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return {}


def populate_from_jwt(cfg: ClientConfig, access_token: str) -> None:
    """Парсит JWT и заполняет cfg permissions/role_id/company_id/expires_at."""
    claims = decode_jwt_claims(access_token)
    if not claims:
        return
    perms = claims.get("permissions") or []
    if isinstance(perms, list):
        cfg.permissions = [str(p) for p in perms]
    cfg.role_id = claims.get("role_id") if claims.get("role_id") else None  # type: ignore[assignment]
    cfg.company_id = claims.get("company_id") if claims.get("company_id") else None  # type: ignore[assignment]
    cfg.is_hub_admin = "hub.admin" in cfg.permissions
    cfg.is_skill_creator = "skill.publish" in cfg.permissions
    exp = claims.get("exp")
    if isinstance(exp, int):
        cfg.access_expires_at = datetime.fromtimestamp(exp, UTC).isoformat()


def _try_keyring() -> object | None:
    try:
        import keyring

        return keyring
    except Exception:
        return None


def save_tokens(user_email: str, access: str, refresh: str) -> None:
    kr = _try_keyring()
    if kr is None:
        path = _default_config_dir() / "tokens.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            tomli_w.dumps({"user_email": user_email, "access": access, "refresh": refresh}),
            encoding="utf-8",
        )
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return
    kr.set_password(_keyring_namespace(), f"{user_email}:access", access)  # type: ignore[attr-defined]
    kr.set_password(_keyring_namespace(), f"{user_email}:refresh", refresh)  # type: ignore[attr-defined]


def load_tokens(user_email: str) -> tuple[str | None, str | None]:
    env_access = os.environ.get("SKILLS_HUB_ACCESS_TOKEN")
    if env_access:
        return env_access, os.environ.get("SKILLS_HUB_REFRESH_TOKEN")

    kr = _try_keyring()
    if kr is None:
        path = _default_config_dir() / "tokens.toml"
        if not path.exists():
            return None, None
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("access"), data.get("refresh")
    access = kr.get_password(_keyring_namespace(), f"{user_email}:access")  # type: ignore[attr-defined]
    refresh = kr.get_password(_keyring_namespace(), f"{user_email}:refresh")  # type: ignore[attr-defined]
    return access, refresh


def clear_tokens(user_email: str) -> None:
    kr = _try_keyring()
    if kr is None:
        (_default_config_dir() / "tokens.toml").unlink(missing_ok=True)
        return
    for key in ("access", "refresh"):
        try:
            kr.delete_password(_keyring_namespace(), f"{user_email}:{key}")  # type: ignore[attr-defined]
        except Exception:
            pass
