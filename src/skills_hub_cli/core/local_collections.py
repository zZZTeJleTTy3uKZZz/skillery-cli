"""Re-export shim: локальные коллекции навыков переехали в кит.

cli-kits W7 (добивка гибрида): CRUD локальных коллекций поверх
``<config_dir>/collections.toml`` теперь в ките ``skillkit.collections`` (0
завязок на сеть/auth/config). Здесь остаётся ТОЛЬКО CLI-специфика — деривация
config-каталога из конфига CLI (``config._default_config_dir`` уважает
``SKILLS_HUB_CONFIG_DIR`` и профили ``--profile``/``SKILLS_HUB_PROFILE``). Эта
логика инъектируется в кит провайдером (``set_config_dir_provider``) — кит сам
env/config НЕ читает.

Shim — АЛИАС kit-модуля в ``sys.modules`` (как ``core/linker.py``): чтобы
``local_collections.LocalCollectionError`` / ``collections_path`` и пр. в
``commands/collection.py`` и тестах указывали на тот же объект, что кит.
"""
from __future__ import annotations

import sys
from pathlib import Path

from skillkit import collections as _kit_collections


def _cli_config_dir() -> Path:
    """Config-каталог CLI (уважает SKILLS_HUB_CONFIG_DIR + профиль)."""
    # Лениво, чтобы профиль/env читались на момент вызова, а не импорта.
    from skills_hub_cli.config import _default_config_dir

    return _default_config_dir()


# Инъектируем CLI-провайдер config-каталога в кит (кит сам env/config не читает).
_kit_collections.set_config_dir_provider(_cli_config_dir)

# Алиас: подменяем этот модуль на kit-модуль, чтобы attribute lookup был общим.
sys.modules[__name__] = _kit_collections
