"""Тонкий re-export shim: построение manifest переехало в кит ``skillkit.manifest``.

cli-kits W7: build_manifest + ридеры frontmatter / _skill_meta.toml теперь в
ките. Здесь — реэкспорт (включая приватные ридеры ``_read_frontmatter`` /
``_read_meta_toml``, которые использует CLI/тесты), чтобы прежние импорты не
ломались.
"""
from __future__ import annotations

from skillkit.manifest import (  # noqa: F401
    BuiltManifest,
    _read_frontmatter,
    _read_meta_toml,
    _sha256_of,
    _should_skip,
    build_manifest,
    git_commit_sha,
)
