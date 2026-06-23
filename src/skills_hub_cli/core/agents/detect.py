"""Re-export shim: детект агента переехал в кит ``skillkit.targets.detect``.

Alias-через-sys.modules: чтобы ``detect_mod.ClaudeCodeTarget`` и
``monkeypatch.setattr(detect_mod.X, ...)`` работали как раньше, этот модуль —
тот же объект, что ``skillkit.targets.detect`` (а не его копия).
"""
from __future__ import annotations

import sys

from skillkit.targets import detect as _kit_detect

sys.modules[__name__] = _kit_detect
