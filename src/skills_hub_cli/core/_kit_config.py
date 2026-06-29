"""Единая брендо-конфигурация **skillery** для кита ``skillkit``.

Один вызов ``skillkit.configure(SkillkitConfig(...))`` вместо разрозненных
``set_*_provider`` по модулям. Здесь собран весь skillery-специфичный бренд:

- ``app_name = "skillery"`` — нативные platformdirs-каталоги + брендо-строки
  (rc-маркер PATH, clone-префикс, владелец управляемых папок);
- проектный манифест ``.skillery/skills.toml`` (write) + legacy-fallback
  ``.skills-hub/skills.toml`` (read) — старые проекты не теряются;
- ленивые провайдеры config/bin-каталогов (уважают ``SKILLS_HUB_CONFIG_DIR`` /
  ``SKILLS_HUB_BIN_DIR`` / профиль ``--profile`` на момент вызова, не импорта).

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

# Канонический (write) путь проектного манифеста — новый бренд; чтение
# fallback'ит на legacy ``.skills-hub`` (старые проекты продолжают читаться).
_MANIFEST_REL = Path(".skillery") / "skills.toml"
_LEGACY_MANIFEST_REL = Path(".skills-hub") / "skills.toml"


def _cli_config_dir() -> Path:
    """Config-каталог CLI (уважает ``SKILLS_HUB_CONFIG_DIR`` + профиль). Лениво."""
    # Лениво, чтобы профиль/env читались на момент вызова, а не импорта.
    from skills_hub_cli.config import _default_config_dir

    return _default_config_dir()


def _cli_bin_dir() -> Path:
    """Bin-каталог CLI: env ``SKILLS_HUB_BIN_DIR`` > ``<store>/../bin``. Лениво."""
    override = os.environ.get("SKILLS_HUB_BIN_DIR")
    if override:
        return Path(override).expanduser()
    from skills_hub_cli.config import _default_store_dir

    return _default_store_dir().parent / "bin"


_CONFIGURED = False


def ensure_configured() -> None:
    """Идемпотентно сконфигурировать кит под бренд skillery (один раз/процесс)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    configure(
        SkillkitConfig(
            app_name="skillery",
            manifest_rel=_MANIFEST_REL,
            manifest_fallbacks=(_LEGACY_MANIFEST_REL,),
            config_dir_provider=_cli_config_dir,
            bin_dir_provider=_cli_bin_dir,
        )
    )
    _CONFIGURED = True


# Сайд-эффект импорта: настроить кит немедленно.
ensure_configured()
