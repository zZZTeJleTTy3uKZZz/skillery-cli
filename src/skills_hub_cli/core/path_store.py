"""Re-export shim: локальный стор CLI-шимов навыков + PATH переехал в кит.

cli-kits W7 (добивка гибрида): вся ФС-механика CLI-шимов (``add_cli`` /
``remove_cli`` / ``ensure_on_path`` / ``list_clis`` + кроссплатформенный PATH)
теперь в SIBLING-ките ``skillkit.path_store`` (0 завязок на сеть/auth/config).

Здесь остаётся ТОЛЬКО CLI-специфика — деривация bin-каталога из конфига/env:
``SKILLS_HUB_BIN_DIR`` > ``effective_store_dir().parent / 'bin'``. Эта логика
инъектируется в кит провайдером (``set_bin_dir_provider``) при импорте — кит сам
env/config НЕ читает.

Чтобы ``monkeypatch.setattr(path_store, "IS_WINDOWS"/"ensure_on_path"/...)`` в
тестах и вызовы из ``commands/doctor.py`` / ``skillkit.tooling`` указывали на
ОДИН объект, этот shim — НЕ копия, а АЛИАС kit-модуля в ``sys.modules`` (как
``core/linker.py``).
"""
from __future__ import annotations

import sys

from skillkit import path_store as _kit_path_store

# Конфигурируем кит под бренд skillery ОДНИМ местом (bin_dir-провайдер и прочее —
# в core/_kit_config). Импорт = сайд-эффект skillkit.configure(...).
from skills_hub_cli.core import _kit_config  # noqa: F401

# Алиас: подменяем этот модуль на kit-модуль, чтобы attribute lookup был общим
# (монкипатч ``path_store.IS_WINDOWS`` и пр. виден и киту, и потребителям).
sys.modules[__name__] = _kit_path_store
