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

import os
import sys
from pathlib import Path

from skillkit import path_store as _kit_path_store


def _cli_bin_dir() -> Path:
    """Каталог шимов CLI: env ``SKILLS_HUB_BIN_DIR`` > ``<store>/../bin``."""
    override = os.environ.get("SKILLS_HUB_BIN_DIR")
    if override:
        return Path(override).expanduser()
    # Лениво, чтобы не тянуть config на уровне модуля (симметрия installer).
    from skills_hub_cli.config import _default_store_dir

    return _default_store_dir().parent / "bin"


# Инъектируем CLI-провайдер bin-каталога в кит (кит сам env/config не читает).
_kit_path_store.set_bin_dir_provider(_cli_bin_dir)

# Алиас: подменяем этот модуль на kit-модуль, чтобы attribute lookup был общим
# (монкипатч ``path_store.IS_WINDOWS`` и пр. виден и киту, и потребителям).
sys.modules[__name__] = _kit_path_store
