"""Re-export shim: оркестрация артефактов навыка (CLI/MCP/deps) переехала в кит.

cli-kits W7 (добивка гибрида): ``apply_tooling_artifacts`` /
``revert_tooling_artifacts`` (runtime_deps → CLI-шимы + PATH → MCP) теперь в
ките ``skillkit.tooling`` (0 завязок на сеть/auth/config). Здесь — АЛИАС
kit-модуля в ``sys.modules`` (как ``core/linker.py``).

ВАЖНО: импортируем ``core.path_store`` ПЕРЕД алиасом — его импорт инъектирует в
кит CLI-провайдер bin-каталога (``SKILLERY_BIN_DIR`` / store-parent), без
которого кит ушёл бы в нативную раскладку platformdirs.
"""
from __future__ import annotations

import sys

from skillkit import tooling as _kit_tooling

# Триггерим инъекцию CLI-провайдера bin-каталога в kit.path_store (side-effect
# импорта shim'а), чтобы tooling клал шимы в правильный каталог CLI.
from skillery_cli.core import path_store as _path_store_shim  # noqa: F401

sys.modules[__name__] = _kit_tooling
