"""Конфигурация клиента: ~/.skills-hub/[profiles/<name>/]config.toml + keyring токены."""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
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


def _default_store_dir() -> Path:
    return Path(
        os.environ.get("SKILLS_HUB_STORE_DIR", "~/.skills-hub/store")
    ).expanduser()


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
    """Permission keys из JWT (используется для модульной регистрации команд).

    «hub-admin»/«skill-creator» больше НЕ хранятся отдельными флагами — после
    wave-B права идут от глобальных memberships и приходят в JWT как обычные
    permission-ключи (`hub.admin`, `skill.publish`). Признаки роли выводятся из
    этого набора методами `is_hub_admin()` / `is_skill_creator()`.
    """
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
    default_install_scope: str = "project"
    """global | project — куда install ставит skill по умолчанию (без --scope).
    Дефолт теперь project: навык включается в текущий проект через стор+ссылку
    и пишется в .skills-hub/skills.toml.
    global → ~/.claude/skills/<id-или-slug>/  (видны во всех проектах)
    project → <project>/.claude/skills/<id-или-slug>/  (только в указанном проекте)
    """
    default_project_dir: str | None = None
    """Дефолтный project root для scope=project. Если null — текущий cwd."""
    store_dir: str | None = None
    """Путь центрального стора навыков. None → SKILLS_HUB_STORE_DIR / ~/.skills-hub/store.
    Навык материализуется в стор один раз; в scope кладётся junction/symlink на него."""
    web_ui_url: str | None = None
    """https://hub.example — куда CLI открывает браузер для handoff в Web UI.
    None → derive из base_url (api.* → host, localhost:8000 → localhost:3000)."""

    def __post_init__(self) -> None:
        if not self.base_url:
            self.base_url = _default_base_url()

    def effective_store_dir(self) -> Path:
        if self.store_dir:
            return Path(self.store_dir).expanduser()
        return _default_store_dir()

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
            company_id=data.get("company_id"),
            role_id=data.get("role_id"),
            access_expires_at=data.get("access_expires_at"),
            auto_update=bool(data.get("auto_update", True)),
            auto_update_cooldown_min=int(data.get("auto_update_cooldown_min", 60)),
            last_auto_update_at=data.get("last_auto_update_at"),
            output_format=str(data.get("output_format", "text")),
            default_install_scope=str(data.get("default_install_scope", "project")),
            default_project_dir=data.get("default_project_dir"),
            store_dir=data.get("store_dir"),
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
        if self.store_dir:
            data["store_dir"] = self.store_dir
        if self.web_ui_url:
            data["web_ui_url"] = self.web_ui_url
        actual_path.write_text(tomli_w.dumps(data), encoding="utf-8")

    def has_permission(self, permission_key: str) -> bool:
        if "hub.admin" in self.permissions:
            return True
        return permission_key in self.permissions

    def is_hub_admin(self) -> bool:
        """hub-admin = наличие права `hub.admin` в эффективном наборе (не флаг)."""
        return "hub.admin" in self.permissions

    def is_skill_creator(self) -> bool:
        """skill-creator = наличие права `skill.publish` в эффективном наборе."""
        return "skill.publish" in self.permissions

    def can_admin_users(self) -> bool:
        """Может ли актор админить участников — зеркало backend-предиката
        ``_can_admin_users`` (routes/users.py).

        S3 D2.2: hub-admin ИЛИ любое из company.manage / user.invite /
        user.remove / user.create / user.update / user.delete / user.lock.
        Раньше CLI гейтил lock/unlock РОВНО на ``user.lock`` → owner
        (company.manage) и manager (user.invite) не видели команду, хотя бэк
        (и UI) их допускают. Единый предикат держит CLI-видимость = backend.
        """
        if self.is_hub_admin():
            return True
        return any(
            p in self.permissions
            for p in (
                "company.manage",
                "user.invite",
                "user.remove",
                "user.create",
                "user.update",
                "user.delete",
                "user.lock",
            )
        )

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
    exp = claims.get("exp")
    if isinstance(exp, int):
        cfg.access_expires_at = datetime.fromtimestamp(exp, UTC).isoformat()


def _try_keyring() -> object | None:
    try:
        import keyring

        return keyring
    except Exception:
        return None


def _tokens_file_path() -> Path:
    return _default_config_dir() / "tokens.toml"


def _write_tokens_file(user_email: str, access: str, refresh: str) -> None:
    """File-fallback хранения токенов: tokens.toml + chmod 600 (best-effort)."""
    path = _tokens_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        tomli_w.dumps({"user_email": user_email, "access": access, "refresh": refresh}),
        encoding="utf-8",
    )
    with contextlib.suppress(Exception):
        os.chmod(path, 0o600)


def _read_tokens_file() -> tuple[str | None, str | None]:
    path = _tokens_file_path()
    if not path.exists():
        return None, None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None, None
    return data.get("access"), data.get("refresh")


def _warn_stderr(message: str) -> None:
    """Один аккуратный warning в stderr (НЕ traceback).

    В json-режиме stderr — это JSON Lines (контракт output.py), поэтому
    предупреждение оборачивается в {"event":"warn",...}; в text — плоская
    строка. Импорт output — лениво и в try/except, чтобы config оставался
    автономным (используется и до инициализации output).
    """
    one_line = " ".join(message.split())
    json_mode = False
    try:
        from skills_hub_cli.output import is_json

        json_mode = is_json()
    except Exception:
        pass
    if json_mode:
        print(
            json.dumps({"event": "warn", "message": one_line}, ensure_ascii=False),
            file=sys.stderr,
        )
    else:
        print(f"! {one_line}", file=sys.stderr)


def save_tokens(user_email: str, access: str, refresh: str) -> None:
    kr = _try_keyring()
    if kr is None:
        _write_tokens_file(user_email, access, refresh)
        return
    ns = _keyring_namespace()
    try:
        kr.set_password(ns, f"{user_email}:access", access)  # type: ignore[attr-defined]
        kr.set_password(ns, f"{user_email}:refresh", refresh)  # type: ignore[attr-defined]
    except Exception as e:
        # Windows Credential Manager ограничивает blob 2560 байт (UTF-16 →
        # ~1280 символов): длинные JWT дают CredWrite WinError 1783. Любая
        # ошибка записи → file-fallback ОБОИХ токенов. При partial-write
        # (access записался, refresh упал) НЕ оставляем рассинхрон: возможно
        # записанные keyring-ключи удаляются best-effort.
        for key in ("access", "refresh"):
            with contextlib.suppress(Exception):
                kr.delete_password(ns, f"{user_email}:{key}")  # type: ignore[attr-defined]
        _write_tokens_file(user_email, access, refresh)
        _warn_stderr(
            f"keyring недоступен для записи токенов ({e.__class__.__name__}: {e}); "
            f"токены сохранены в файл {_tokens_file_path()}"
        )
        return
    # Запись в keyring успешна — подчищаем устаревший file-fallback, чтобы
    # load_tokens при недоступном keyring не вернул СТАРУЮ пару токенов.
    with contextlib.suppress(Exception):
        _tokens_file_path().unlink(missing_ok=True)


def load_tokens(user_email: str) -> tuple[str | None, str | None]:
    env_access = os.environ.get("SKILLS_HUB_ACCESS_TOKEN")
    if env_access:
        return env_access, os.environ.get("SKILLS_HUB_REFRESH_TOKEN")

    kr = _try_keyring()
    if kr is None:
        return _read_tokens_file()
    access: str | None = None
    refresh: str | None = None
    try:
        access = kr.get_password(_keyring_namespace(), f"{user_email}:access")  # type: ignore[attr-defined]
        refresh = kr.get_password(_keyring_namespace(), f"{user_email}:refresh")  # type: ignore[attr-defined]
    except Exception:
        access, refresh = None, None
    if access is None or refresh is None:
        # keyring установлен, но запись могла уйти в file-fallback (например
        # CredWrite WinError 1783 на длинных JWT) — дочитываем tokens.toml.
        file_access, file_refresh = _read_tokens_file()
        access = access if access is not None else file_access
        refresh = refresh if refresh is not None else file_refresh
    return access, refresh


def clear_tokens(user_email: str) -> None:
    """Чистит ОБА хранилища best-effort: keyring-ключи и file-fallback."""
    kr = _try_keyring()
    if kr is not None:
        for key in ("access", "refresh"):
            with contextlib.suppress(Exception):
                kr.delete_password(_keyring_namespace(), f"{user_email}:{key}")  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        _tokens_file_path().unlink(missing_ok=True)
