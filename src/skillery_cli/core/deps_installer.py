"""Re-export shim: установка runtime-зависимостей навыка переехала в кит.

cli-kits W7 (добивка гибрида): ``install_runtime_dependencies`` (pip via uv→pip,
npm, system) теперь в ките ``skillkit.deps_installer`` (0 завязок на сеть/auth/
config — только subprocess/shutil). Здесь — АЛИАС kit-модуля в ``sys.modules``
(как ``core/linker.py``), чтобы ``monkeypatch.setattr(deps_installer, "_run"/
"_which"/...)`` в тестах и вызовы из ``skillkit.tooling`` указывали на один
объект.
"""
from __future__ import annotations

import sys

from skillkit import deps_installer as _kit_deps_installer

sys.modules[__name__] = _kit_deps_installer
