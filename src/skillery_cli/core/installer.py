"""Тонкий re-export shim: ядро установщика навыков переехало в кит ``skillkit``.

cli-kits W7: вся ФС-механика навыка (материализация в стор, junction/symlink
линковка, инкрементальный sha-diff update, stub-would-clobber guard) теперь в
SIBLING-ките ``skillkit`` (0 завязок на сеть/auth). Здесь остаётся:

- ``_authenticated_url`` — CLI-специфичный резолвер git-учётки (читает env
  ``SKILLERY_GIT_TOKEN`` / ``GITLAB_TOKEN``); кит сам env НЕ читает;
- ``SkillInstaller`` — тонкая обёртка над ``skillkit.SkillStore``, которая
  автоматически инъектирует этот резолвер (сохраняет историческую сигнатуру
  ``SkillInstaller(target, store_dir)`` для всех call-sites и тестов).

Остальное (``read_meta`` / ``write_meta`` / ``safe_copy_tree`` /
``skill_dir_name`` / ``PathTraversalError`` / ``InstallResult`` /
``RemoveResult`` / приватные диффы и guard'ы) реэкспортируется из кита 1:1 —
чтобы прежние импорты ``from skillery_cli.core.installer import ...`` не
ломались.
"""
from __future__ import annotations

import os
import subprocess  # noqa: F401  (re-export: тесты патчат installer.subprocess.run)
from pathlib import Path

from skillkit.installer import (  # noqa: F401  (re-export для обратной совместимости)
    InstallResult,
    PathTraversalError,
    RemoveResult,
    SkillStore,
    _DEFAULT_PRESERVED,
    _SKILL_META_FILE,
    _STUB_SENTINEL,
    _assert_within,
    _file_index,
    _force_rmtree,
    _is_preserved_rel,
    _manifest_diff,
    _on_rm_error,
    _preserved_for,
    _prune_empty_parents,
    _safe_copy_file,
    _stub_would_clobber,
    _version_from_skill_md,
    read_meta,
    safe_copy_tree,
    skill_dir_name,
    write_meta,
)


def _authenticated_url(url: str) -> str:
    """CLI-резолвер git-учётки: подставляет env-токен в https-URL.

    Кит ``skillkit`` сам env НЕ читает — токен инъектируется этим резолвером
    (см. ``SkillInstaller``). Поведение сохранено 1:1 с прежним кодом.
    """
    if "@" in url or not url.startswith("https://"):
        return url
    from skillery_cli import _branding

    token = os.environ.get(_branding.env("GIT_TOKEN")) or os.environ.get("GITLAB_TOKEN")
    if not token:
        return url
    return url.replace("https://", f"https://oauth2:{token}@", 1)


class SkillInstaller(SkillStore):
    """Обёртка над ``skillkit.SkillStore`` с CLI-резолвером git-учётки.

    Сохраняет историческую сигнатуру: ``SkillInstaller(target, store_dir)``.
    ``store_dir`` обязателен (все call-sites передают ``cfg.effective_store_dir()``).
    """

    def __init__(self, target, store_dir: Path, *, credential_resolver=None) -> None:
        super().__init__(
            target,
            store_dir,
            credential_resolver=credential_resolver or _authenticated_url,
        )
