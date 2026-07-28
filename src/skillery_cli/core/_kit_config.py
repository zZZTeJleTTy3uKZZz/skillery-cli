"""Единая брендо-конфигурация **skillery** для кита ``skillkit``.

Один вызов ``skillkit.configure(SkillkitConfig(...))`` вместо разрозненных
``set_*_provider`` по модулям. Здесь собран весь skillery-специфичный бренд:

- ``app_name = "skillery"`` — нативные platformdirs-каталоги + брендо-строки
  (rc-маркер PATH, clone-префикс, владелец управляемых папок);
- проектный манифест ``.skillery/skills.toml``;
- ленивые провайдеры config/bin-каталогов (уважают ``SKILLERY_CONFIG_DIR`` /
  ``SKILLERY_BIN_DIR`` / профиль ``--profile`` на момент вызова, не импорта).

Импортируется как сайд-эффект шимами ``core/{project_manifest,
local_collections,path_store}.py``: какой бы ни загрузился первым — кит
конфигурируется целиком и идемпотентно (``configure`` перенастраивает всё).

Требует ``s-skillkit>=0.2.0`` (несёт ``SkillkitConfig``/``configure``). На более
старом ките импорт упал бы ImportError — это явный сигнал обновить кит, а не
тихий no-op прежней ``hasattr``-заглушки.
"""
from __future__ import annotations

import os
from pathlib import Path

from skillkit import SkillkitConfig, configure

from skillery_cli import _branding

# Путь проектного манифеста.
_MANIFEST_REL = Path(_branding.HOME_DIR_NAME) / "skills.toml"


def _cli_config_dir() -> Path:
    """Config-каталог CLI (уважает env <PREFIX>_CONFIG_DIR + профиль). Лениво."""
    # Лениво, чтобы профиль/env читались на момент вызова, а не импорта.
    from skillery_cli.config import _default_config_dir

    return _default_config_dir()


def _cli_bin_dir() -> Path:
    """Bin-каталог CLI: env <PREFIX>_BIN_DIR > ``<store>/../bin``. Лениво."""
    override = os.environ.get(_branding.env("BIN_DIR"))
    if override:
        return Path(override).expanduser()
    from skillery_cli.config import _default_store_dir

    return _default_store_dir().parent / "bin"


_CONFIGURED = False


def ensure_configured() -> None:
    """Идемпотентно сконфигурировать кит под бренд skillery (один раз/процесс)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    configure(
        SkillkitConfig(
            app_name=_branding.APP_NAME,
            manifest_rel=_MANIFEST_REL,
            config_dir_provider=_cli_config_dir,
            bin_dir_provider=_cli_bin_dir,
        )
    )
    # Телеметрия: s-telemetrykit НЕЙТРАЛЕН (дефолт ~/.telemetrykit, env
    # TELEMETRYKIT_*), а skillery-настройки живут в ките ПРОДУКТА. Импорт
    # `skillkit.telemetry` — и есть их применение (home=~/.skillery,
    # env_prefix=SKILLERY, kind=skill_run).
    #
    # Почему это ОБЯЗАТЕЛЬНО здесь: навыки пишут события через
    # `skillkit.telemetry`, то есть в ~/.skillery/outbox.jsonl. Если CLI не
    # применит ту же конфигурацию, его воркер-доставщик будет читать
    # нейтральный ~/.telemetrykit/outbox.jsonl — очередей снова станет ДВЕ,
    # и события навыков не уедут на сервер вообще (молча).
    #
    # Импортируем адаптер, а не зовём telemetrykit.configure() своими руками:
    # иначе настройки Skillery задавались бы в двух местах и разъехались бы.
    from skillkit import telemetry as _telemetry  # noqa: F401

    _CONFIGURED = True


# Сайд-эффект импорта: настроить кит немедленно.
ensure_configured()
