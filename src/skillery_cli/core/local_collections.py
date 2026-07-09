"""Re-export shim: локальные коллекции навыков переехали в кит.

cli-kits W7 (добивка гибрида): CRUD локальных коллекций поверх
``<config_dir>/collections.toml`` теперь в ките ``skillkit.collections`` (0
завязок на сеть/auth/config). Здесь остаётся ТОЛЬКО CLI-специфика — деривация
config-каталога из конфига CLI (``config._default_config_dir`` уважает
``SKILLERY_CONFIG_DIR`` и профили ``--profile``/``SKILLERY_PROFILE``). Эта
логика инъектируется в кит провайдером (``set_config_dir_provider``) — кит сам
env/config НЕ читает.

Shim — АЛИАС kit-модуля в ``sys.modules`` (как ``core/linker.py``): чтобы
``local_collections.LocalCollectionError`` / ``collections_path`` и пр. в
``commands/collection.py`` и тестах указывали на тот же объект, что кит.
"""
from __future__ import annotations

import sys

from skillkit import collections as _kit_collections

# Конфигурируем кит под бренд skillery ОДНИМ местом (config_dir-провайдер и
# прочее — в core/_kit_config). Импорт = сайд-эффект skillkit.configure(...).
from skillery_cli.core import _kit_config  # noqa: F401

# Алиас: подменяем этот модуль на kit-модуль, чтобы attribute lookup был общим.
sys.modules[__name__] = _kit_collections
