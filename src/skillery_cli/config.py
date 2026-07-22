"""Конфигурация клиента: ~/.skillery/[profiles/<name>/]config.toml + keyring токены.

Дефолтный home-каталог конфига и центрального стора — ``~/.skillery``. env-override
``SKILLERY_CONFIG_DIR`` / ``SKILLERY_STORE_DIR`` имеют наивысший приоритет.
"""
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
from clikit.config import AppConfig, interpolate_env
from librarykit.config_util import atomic_write_text as _atomic_write_text
from librarykit.secret_store import SecretStore
from pydantic import Field

from skillery_cli import _branding

KEYRING_SERVICE = _branding.DIST_NAME

# Текущий активный профиль (--profile / <PREFIX>_PROFILE env).
_ACTIVE_PROFILE: str | None = None


def set_active_profile(profile: str | None) -> None:
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = profile or os.environ.get(_branding.env("PROFILE"))


def active_profile() -> str | None:
    return _ACTIVE_PROFILE or os.environ.get(_branding.env("PROFILE"))


def _resolve_home_base(env_var: str, default: str) -> Path:
    """База home-каталога: env-override (если задан) либо дефолт ``~/<home>``."""
    env_val = os.environ.get(env_var)
    if env_val:
        return Path(env_val).expanduser()
    return Path(default).expanduser()


def _default_config_dir() -> Path:
    base = _resolve_home_base(
        _branding.env("CONFIG_DIR"), f"~/{_branding.HOME_DIR_NAME}"
    )
    profile = active_profile()
    if profile:
        return base / "profiles" / profile
    return base


def _default_config_file() -> Path:
    return _default_config_dir() / "config.toml"


def _default_base_url() -> str:
    return os.environ.get(_branding.env("BASE_URL"), _branding.DEFAULT_BASE_URL)


def _default_web_ui_url() -> str:
    return os.environ.get(_branding.env("WEB_UI_URL"), _branding.DEFAULT_WEB_UI_URL)


def _default_store_dir() -> Path:
    return _resolve_home_base(
        _branding.env("STORE_DIR"), f"~/{_branding.HOME_DIR_NAME}/store"
    )


def _keyring_namespace() -> str:
    profile = active_profile()
    return f"{KEYRING_SERVICE}:{profile}" if profile else KEYRING_SERVICE


# Совместимость со старым API (lazy для тестов)
DEFAULT_CONFIG_DIR = _default_config_dir()
DEFAULT_CONFIG_FILE = _default_config_file()
DEFAULT_BASE_URL = _default_base_url()


class _HubAppConfig(AppConfig):
    """``clikit.config.AppConfig`` для конфиг-слоя ClientConfig (cli-kits W4).

    Несёт все ПЕРСИСТИМЫЕ поля ClientConfig как typed-поля pydantic — это
    переводит конфиг-слой CLI на каноничную модель кита и даёт fail-fast
    валидацию типов из коробки (``extra='ignore'`` базового AppConfig делает
    load forward-compatible: незнакомые/legacy-ключи в TOML игнорируются, а не
    падают). ``ClientConfig.load`` инстанцирует эту модель через
    ``model_validate`` (после BOM-safe чтения файла + ``interpolate_env``), а
    ``ClientConfig.save`` пишет TOML атомарно (writer кита).

    Поля-предикаты (``is_hub_admin`` и т.п.), derive-методы и токены остаются на
    ClientConfig — это лишь сериализационный слой. ``output_format`` объявлен с
    дефолтом ``"text"`` (а не ``None`` базового AppConfig) — историческое
    значение CLI; пустой ``base_url`` доводится до env-дефолта
    (``SKILLERY_BASE_URL``) уже в ClientConfig ``__post_init__``.
    """

    # base_url / output_format / mcp / adapters унаследованы от AppConfig;
    # переопределяем дефолт output_format на исторический "text".
    output_format: str = "text"
    user_email: str | None = None
    user_display_name: str | None = None
    agent: str | None = None
    permissions: list[str] = Field(default_factory=list)
    company_id: str | None = None
    role_id: str | None = None
    access_expires_at: str | None = None
    auto_update: bool = True
    auto_update_cooldown_min: int = 60
    last_auto_update_at: str | None = None
    default_install_scope: str = "project"
    default_project_dir: str | None = None
    store_dir: str | None = None
    web_ui_url: str | None = None
    cli_update_check: bool = True
    cli_update_check_at: str | None = None
    cli_latest_version: str | None = None
    cli_auto_upgrade: bool = True
    log_level: str = "error"


@dataclass
class ClientConfig:
    base_url: str = ""
    user_email: str | None = None
    user_display_name: str | None = None
    log_level: str = "error"
    """Уровень логов CLI (error|warning|info|debug|trace). Пишутся в
    ~/.skillery/logs/. По умолчанию error; debug/trace — для разбора."""
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
    Можно переопределить через --json флаг или env SKILLERY_OUTPUT=json.
    Для AI агентов рекомендуется json."""
    default_install_scope: str = "project"
    """global | project — куда install ставит skill по умолчанию (без --scope).
    Дефолт теперь project: навык включается в текущий проект через стор+ссылку
    и пишется в .skillery/skills.toml.
    global → ~/.claude/skills/<id-или-slug>/  (видны во всех проектах)
    project → <project>/.claude/skills/<id-или-slug>/  (только в указанном проекте)
    """
    default_project_dir: str | None = None
    """Дефолтный project root для scope=project. Если null — текущий cwd."""
    store_dir: str | None = None
    """Путь центрального стора навыков. None → SKILLERY_STORE_DIR / ~/.skillery/store.
    Навык материализуется в стор один раз; в scope кладётся junction/symlink на него."""
    web_ui_url: str | None = None
    """https://hub.example — куда CLI открывает браузер для handoff в Web UI.
    None → derive из base_url (api.* → host, localhost:8000 → localhost:3000)."""
    cli_update_check: bool = True
    """Проверять ли наличие новой версии самого CLI на PyPI (уведомление + `upgrade`)."""
    cli_update_check_at: str | None = None
    """iso-таймстамп последней проверки версии CLI (кэш, чтобы не бить PyPI чаще раза/сутки)."""
    cli_latest_version: str | None = None
    """Последняя виденная на PyPI версия CLI (кэш для уведомления в пределах cooldown)."""
    cli_auto_upgrade: bool = True
    """Само-обновление CLI: при обнаружении новой версии тихо обновиться в фоне
    (uv tool / pipx / pip). False → только уведомление + ручной `skillery upgrade`."""

    def __post_init__(self) -> None:
        if not self.base_url:
            self.base_url = _default_base_url()

    def effective_store_dir(self) -> Path:
        if self.store_dir:
            return Path(self.store_dir).expanduser()
        return _default_store_dir()

    def effective_web_ui_url(self) -> str:
        """URL Web UI: explicit override > прод-дефолт > dev-derive > эвристика.

        Порядок важен: прод-API ``api.skillery.ru`` и веб ``hub.skillery.ru`` —
        разные субдомены, деривацией «убрать api.» из base НЕ выводятся (дала бы
        ``skillery.ru``). Поэтому default base → default web_ui проверяется ДО
        эвристики ``api.<host> → <host>`` (та для self-host паттерна api.hub.*).
        """
        if self.web_ui_url:
            return self.web_ui_url
        base = self.base_url
        # Прод-дефолт (или его env-оверрайд): api.skillery.ru ↔ hub.skillery.ru.
        if base.rstrip("/") == _default_base_url().rstrip("/"):
            return _default_web_ui_url()
        # dev: http://localhost:8000 → http://localhost:3000 (Next.js dev).
        if "localhost:8000" in base:
            return base.replace("localhost:8000", "localhost:3000")
        # self-host эвристика: http(s)://api.<host> → http(s)://<host>.
        for scheme in ("https://", "http://"):
            if base.startswith(scheme + "api."):
                return scheme + base[len(scheme) + len("api."):]
        return base

    @classmethod
    def load(cls, path: Path | None = None) -> ClientConfig:
        """Прочитать конфиг поверх ``clikit.config.AppConfig`` (cli-kits W4).

        Конфиг-слой ClientConfig — это модель ``_HubAppConfig`` (подкласс
        ``AppConfig``): даёт fail-fast валидацию типов и env-интерполяцию
        ``${VAR}`` из коробки. Чтение нашего канонического файла
        (``~/.skillery/[profiles/<p>/]config.toml``) делаем сами и BOM-safe
        (``utf-8-sig``) — историческое поведение CLI (некоторые Windows-редакторы
        пишут BOM); затем интерполяция и ``model_validate`` (``extra='ignore'`` —
        незнакомые/устаревшие ключи не ломают load).

        Историческая семантика сохранена: отсутствующий файл → дефолты;
        ``base_url`` из файла приоритетнее env-дефолта ``SKILLERY_BASE_URL``
        (env лишь подставляет дефолт, когда поля нет — это делает
        ``__post_init__`` / ``effective_store_dir``).
        """
        actual_path = path or _default_config_file()
        if not actual_path.exists():
            return cls()
        raw = tomllib.loads(actual_path.read_text(encoding="utf-8-sig"))
        ac = _HubAppConfig.model_validate(interpolate_env(raw))
        base_url = ac.base_url or _default_base_url()
        # Защита от битого/кривого base_url (без схемы) в конфиге: иначе httpx
        # падает криптовым ``UnsupportedProtocol: Request URL is missing an
        # 'http://' or 'https://' protocol`` вместо понятной ошибки. Нет схемы →
        # игнорируем значение, берём прод-дефолт (env SKILLERY_BASE_URL перебьёт).
        if not base_url.startswith(("http://", "https://")):
            base_url = _default_base_url()
        return cls(
            base_url=base_url,
            user_email=ac.user_email,
            user_display_name=ac.user_display_name,
            agent=ac.agent,
            permissions=list(ac.permissions),
            company_id=ac.company_id,
            role_id=ac.role_id,
            access_expires_at=ac.access_expires_at,
            auto_update=bool(ac.auto_update),
            auto_update_cooldown_min=int(ac.auto_update_cooldown_min),
            last_auto_update_at=ac.last_auto_update_at,
            output_format=str(ac.output_format or "text"),
            default_install_scope=str(ac.default_install_scope or "project"),
            default_project_dir=ac.default_project_dir,
            store_dir=ac.store_dir,
            web_ui_url=ac.web_ui_url,
            cli_update_check=bool(ac.cli_update_check),
            cli_update_check_at=ac.cli_update_check_at,
            cli_latest_version=ac.cli_latest_version,
            cli_auto_upgrade=bool(ac.cli_auto_upgrade),
            log_level=str(ac.log_level or "error"),
        )

    def save(self, path: Path | None = None) -> None:
        """Записать конфиг (TOML ``~/.skillery/[profiles/<p>/]config.toml``).

        Формат и набор ключей — байт-в-байт прежние (омит пустых опц.полей),
        запись атомарная (``clikit``/``librarykit`` writer вместо прямого
        ``write_text``). Каталог создаётся при отсутствии.
        """
        actual_path = path or _default_config_file()
        actual_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, object] = {"base_url": self.base_url}
        if self.user_email:
            data["user_email"] = self.user_email
        if self.user_display_name:
            data["user_display_name"] = self.user_display_name
        if self.log_level and self.log_level != "error":
            data["log_level"] = self.log_level
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
        data["cli_update_check"] = self.cli_update_check
        if self.cli_update_check_at:
            data["cli_update_check_at"] = self.cli_update_check_at
        if self.cli_latest_version:
            data["cli_latest_version"] = self.cli_latest_version
        data["cli_auto_upgrade"] = self.cli_auto_upgrade
        _atomic_write_text(actual_path, tomli_w.dumps(data))

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
    """Парсит JWT и заполняет cfg role_id/company_id/access_expires_at.

    JWT-slim (2026-06): permissions БОЛЬШЕ не читаются из токена — развёрнутый
    список прав туда не кладётся. Их источник — ``GET /me/permissions``
    (БД-авторитетно), см. ``commands._common.hydrate_session_permissions``,
    вызываемый login-флоу после получения токена.
    """
    claims = decode_jwt_claims(access_token)
    if not claims:
        return
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
        from skillery_cli.output import is_json

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


class _HubSecretStore(SecretStore):
    """``librarykit.SecretStore``, привязанный к контракту хранения config.py.

    cli-kits W2: secret-слой токенов CLI — это SecretStore из кита-владельца, а
    не своя копия логики keyring/file-fallback. НО историческая раскладка
    Skills Hub должна остаться байт-в-байт (иначе залогиненный прод-юзер
    разлогинится), поэтому подкласс переопределяет три точки SecretStore так,
    чтобы они шли через существующие config-функции:

    - ``_keyring()``       → ``_try_keyring`` (сохраняет monkeypatch-точку тестов
                             и единый импорт keyring);
    - ``_namespace()``     → ``_keyring_namespace`` (namespace остаётся
                             ``skillery-cli[:<profile>]``, а не ``brand`` от
                             конструктора — старые keyring-ключи читаются);
    - ``_tokens_file_path``→ ``_tokens_file_path`` config.py (путь
                             ``~/.skillery/[profiles/<p>/]tokens.toml`` через
                             резолвер каталога, а не ``platformdirs.user_config_dir``).

    Сами алгоритмы (порядок keyring→file, partial-write cleanup, удаление stale
    файла, дочитывание файла при пустом keyring) — наследуются от SecretStore.
    Ключи токенов (``{user}:access``/``{user}:refresh``) и формат tokens.toml у
    SecretStore идентичны прежним config.py — менять нечего.
    """

    def __init__(self) -> None:
        super().__init__(KEYRING_SERVICE, profile=active_profile())

    def _keyring(self):  # type: ignore[override]
        return _try_keyring()

    def _namespace(self) -> str:  # type: ignore[override]
        return _keyring_namespace()

    def _tokens_file_path(self) -> Path:  # type: ignore[override]
        return _tokens_file_path()


def _secret_store() -> _HubSecretStore:
    """Фабрика стора на текущий профиль (профиль читается из env/`set_active_profile`)."""
    return _HubSecretStore()


def save_tokens(user_email: str, access: str, refresh: str) -> None:
    """Сохранить пару токенов через ``librarykit.SecretStore``.

    SecretStore сам делает: keyring → при ЛЮБОЙ ошибке записи (вкл. CredWrite
    WinError 1783 на длинных JWT) file-fallback обоих токенов + best-effort
    cleanup частично записанных keyring-ключей; успешная запись keyring чистит
    stale tokens.toml. Поверх этого config.py добавляет аккуратный warning в
    stderr (контракт config.py — у generic-стора его нет): если после save
    keyring пуст, а файл появился, значит запись ушла в file-fallback.
    """
    kr = _try_keyring()
    store = _secret_store()
    if kr is None:
        # keyring недоступен вовсе — тихий file-fallback (как раньше, без warn).
        store.save_tokens(user_email, access, refresh)
        return

    file_existed_before = _tokens_file_path().exists()
    store.save_tokens(user_email, access, refresh)

    # SecretStore проглатывает ошибку записи keyring молча. Определяем факт
    # ухода в file-fallback по тому, что в keyring токенов НЕТ, а файл есть.
    ns = _keyring_namespace()
    wrote_keyring = False
    with contextlib.suppress(Exception):
        wrote_keyring = (
            kr.get_password(ns, f"{user_email}:access") is not None  # type: ignore[attr-defined]
        )
    if not wrote_keyring and _tokens_file_path().exists() and not file_existed_before:
        _warn_stderr(
            "keyring недоступен для записи токенов; "
            f"токены сохранены в файл {_tokens_file_path()}"
        )


def load_tokens(user_email: str) -> tuple[str | None, str | None]:
    """Прочитать пару токенов через ``librarykit.SecretStore``.

    env-override (``SKILLERY_ACCESS_TOKEN``/``SKILLERY_REFRESH_TOKEN``)
    читается ПЕРВЫМ — это исторический контракт config.py (SecretStore читает
    env по ``<BRAND>_*``-ключам с дефисами, что под нашим брендом не совпало
    бы), поэтому env-ветку обрабатываем здесь до делегации в стор.
    """
    env_access = os.environ.get(_branding.env("ACCESS_TOKEN"))
    if env_access:
        return env_access, os.environ.get(_branding.env("REFRESH_TOKEN"))
    return _secret_store().load_tokens(user_email)


def clear_tokens(user_email: str) -> None:
    """Чистит ОБА хранилища best-effort: keyring-ключи и file-fallback."""
    _secret_store().clear_tokens(user_email)
