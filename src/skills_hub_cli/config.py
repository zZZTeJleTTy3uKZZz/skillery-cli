"""Конфигурация клиента: ~/.skillery/[profiles/<name>/]config.toml + keyring токены.

Ребренд skills-hub→skillery: дефолтный home-каталог конфига — ``~/.skillery``.
ОБРАТНАЯ СОВМЕСТИМОСТЬ со старыми установками (``~/.skills-hub``): если новый
каталог ещё не создан, а старый существует — резолвер ``_default_config_dir``
возвращает СТАРЫЙ путь (чтобы залогиненный/настроенный юзер не «потерял» свой
config/session при апгрейде CLI). При первой ЗАПИСИ (``ClientConfig.save`` /
``_write_tokens_file``) каталог best-effort мигрируется ``~/.skills-hub`` →
``~/.skillery`` (rename, при неудаче — дальше пишем в новый, старый не трогаем).
env-override ``SKILLS_HUB_CONFIG_DIR`` / ``SKILLS_HUB_STORE_DIR`` остаются
точками переопределения (имена env вне scope ребренда) и имеют наивысший
приоритет — fallback на legacy-каталог при заданном env НЕ применяется.
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

KEYRING_SERVICE = "skills-hub-cli"

# Текущий активный профиль (--profile / SKILLS_HUB_PROFILE).
_ACTIVE_PROFILE: str | None = None


def set_active_profile(profile: str | None) -> None:
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = profile or os.environ.get("SKILLS_HUB_PROFILE")


def active_profile() -> str | None:
    return _ACTIVE_PROFILE or os.environ.get("SKILLS_HUB_PROFILE")


# Имена брендовых home-каталогов конфига (ребренд skills-hub→skillery).
_NEW_HOME_DIR_NAME = ".skillery"
_LEGACY_HOME_DIR_NAME = ".skills-hub"


def _resolve_home_base(env_var: str, new_default: str, legacy_default: str) -> Path:
    """База home-каталога с учётом env-override и legacy-fallback.

    Приоритет:
    1. ``env_var`` (``SKILLS_HUB_CONFIG_DIR`` / ``SKILLS_HUB_STORE_DIR``) — если
       задан, используется как есть (override-точка, fallback НЕ применяется).
    2. Новый дефолт ``~/.skillery[...]`` — если каталог уже существует.
    3. Legacy ``~/.skills-hub[...]`` — если новый ещё НЕ создан, а старый ЕСТЬ
       (обратная совместимость: не теряем config/session старой установки).
    4. Иначе — новый дефолт (свежая установка пишет сразу в ``~/.skillery``).
    """
    env_val = os.environ.get(env_var)
    if env_val:
        return Path(env_val).expanduser()
    new_path = Path(new_default).expanduser()
    if new_path.exists():
        return new_path
    legacy_path = Path(legacy_default).expanduser()
    if legacy_path.exists():
        return legacy_path
    return new_path


def _default_config_dir() -> Path:
    base = _resolve_home_base(
        "SKILLS_HUB_CONFIG_DIR",
        f"~/{_NEW_HOME_DIR_NAME}",
        f"~/{_LEGACY_HOME_DIR_NAME}",
    )
    profile = active_profile()
    if profile:
        return base / "profiles" / profile
    return base


def _default_config_file() -> Path:
    return _default_config_dir() / "config.toml"


def _default_base_url() -> str:
    return os.environ.get("SKILLS_HUB_BASE_URL", "http://localhost:8000")


def _default_store_dir() -> Path:
    return _resolve_home_base(
        "SKILLS_HUB_STORE_DIR",
        f"~/{_NEW_HOME_DIR_NAME}/store",
        f"~/{_LEGACY_HOME_DIR_NAME}/store",
    )


def _maybe_migrate_legacy_home() -> None:
    """Best-effort миграция ``~/.skills-hub`` → ``~/.skillery`` при первой записи.

    Вызывается перед записью config/токенов. Срабатывает ТОЛЬКО когда:
    env-override каталога не задан, новый каталог ``~/.skillery`` ещё не создан,
    а legacy ``~/.skills-hub`` существует. Тогда переносим весь каталог одним
    ``rename`` (атомарно в пределах одной ФС). Любая ошибка глотается — тогда
    запись просто пойдёт в новый каталог (``mkdir(parents=True)`` у
    save/_write_tokens_file его создаст), а legacy остаётся нетронутым (читать
    мы его уже не будем, т.к. новый появится).
    """
    if os.environ.get("SKILLS_HUB_CONFIG_DIR"):
        return
    new_base = Path(f"~/{_NEW_HOME_DIR_NAME}").expanduser()
    if new_base.exists():
        return
    legacy_base = Path(f"~/{_LEGACY_HOME_DIR_NAME}").expanduser()
    if not legacy_base.exists():
        return
    with contextlib.suppress(OSError):
        new_base.parent.mkdir(parents=True, exist_ok=True)
        legacy_base.rename(new_base)


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
    (``SKILLS_HUB_BASE_URL``) уже в ClientConfig ``__post_init__``.
    """

    # base_url / output_format / mcp / adapters унаследованы от AppConfig;
    # переопределяем дефолт output_format на исторический "text".
    output_format: str = "text"
    user_email: str | None = None
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
        """Прочитать конфиг поверх ``clikit.config.AppConfig`` (cli-kits W4).

        Конфиг-слой ClientConfig — это модель ``_HubAppConfig`` (подкласс
        ``AppConfig``): даёт fail-fast валидацию типов и env-интерполяцию
        ``${VAR}`` из коробки. Чтение нашего канонического файла
        (``~/.skillery/[profiles/<p>/]config.toml``, с fallback на legacy
        ``~/.skills-hub`` через резолвер пути) делаем сами и BOM-safe
        (``utf-8-sig``) — историческое поведение CLI (некоторые Windows-редакторы
        пишут BOM); затем интерполяция и ``model_validate`` (``extra='ignore'`` —
        незнакомые/legacy-ключи не ломают load).

        Историческая семантика сохранена: отсутствующий файл → дефолты;
        ``base_url`` из файла приоритетнее env-дефолта ``SKILLS_HUB_BASE_URL``
        (env лишь подставляет дефолт, когда поля нет — это делает
        ``__post_init__`` / ``effective_store_dir``).
        """
        actual_path = path or _default_config_file()
        if not actual_path.exists():
            return cls()
        raw = tomllib.loads(actual_path.read_text(encoding="utf-8-sig"))
        ac = _HubAppConfig.model_validate(interpolate_env(raw))
        base_url = ac.base_url or _default_base_url()
        return cls(
            base_url=base_url,
            user_email=ac.user_email,
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
        )

    def save(self, path: Path | None = None) -> None:
        """Записать конфиг (TOML ``~/.skillery/[profiles/<p>/]config.toml``).

        Формат и набор ключей — байт-в-байт прежние (омит пустых опц.полей),
        запись атомарная (``clikit``/``librarykit`` writer вместо прямого
        ``write_text``). Каталог создаётся при отсутствии.
        """
        if path is None:
            # Дефолтная запись: мигрируем legacy-каталог до резолва пути, чтобы
            # config ушёл уже в ~/.skillery (а не «довывел» новый рядом со старым).
            _maybe_migrate_legacy_home()
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
    # Мигрируем legacy-каталог до резолва пути (session-токены ←→ config-каталог).
    _maybe_migrate_legacy_home()
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
                             ``skills-hub-cli[:<profile>]``, а не ``brand`` от
                             конструктора — старые keyring-ключи читаются);
    - ``_tokens_file_path``→ ``_tokens_file_path`` config.py (путь остаётся
                             ``~/.skillery/[profiles/<p>/]tokens.toml`` — с
                             legacy-fallback ``~/.skills-hub`` через резолвер
                             каталога, а не ``platformdirs.user_config_dir``).

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

    env-override (``SKILLS_HUB_ACCESS_TOKEN``/``SKILLS_HUB_REFRESH_TOKEN``)
    читается ПЕРВЫМ — это исторический контракт config.py (SecretStore читает
    env по ``<BRAND>_*``-ключам с дефисами, что под нашим брендом не совпало
    бы), поэтому env-ветку обрабатываем здесь до делегации в стор.
    """
    env_access = os.environ.get("SKILLS_HUB_ACCESS_TOKEN")
    if env_access:
        return env_access, os.environ.get("SKILLS_HUB_REFRESH_TOKEN")
    return _secret_store().load_tokens(user_email)


def clear_tokens(user_email: str) -> None:
    """Чистит ОБА хранилища best-effort: keyring-ключи и file-fallback."""
    _secret_store().clear_tokens(user_email)
