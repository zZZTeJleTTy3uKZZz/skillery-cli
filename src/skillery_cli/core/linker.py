"""Re-export shim: линковка навыков переехала в кит ``skillkit.linker``.

cli-kits W7. Чтобы ``monkeypatch.setattr(linker, "create_link", ...)`` в тестах
реально влиял на вызовы из ``skillkit.installer`` (которые делают
``from skillkit import linker``), этот shim — НЕ копия, а АЛИАС того же
модуль-объекта: ``skillery_cli.core.linker`` и ``skillkit.linker`` указывают
на один объект в ``sys.modules``. Так патч одного namespace виден другому.
"""
from __future__ import annotations

import sys

from skillkit import linker as _kit_linker

# Алиас: подменяем этот модуль на kit-модуль, чтобы attribute lookup был общим.
sys.modules[__name__] = _kit_linker
