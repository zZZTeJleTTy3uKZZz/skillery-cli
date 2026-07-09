"""Тонкий re-export shim: проектный манифест переехал в кит ``skillkit.project``.

cli-kits W7: ``.skillery/skills.toml`` ридер/райтер теперь в ките. Здесь —
реэкспорт, чтобы прежние импорты ``from skillery_cli.core import
project_manifest`` не ломались.

Проектный манифест — ``.skillery/skills.toml``. Кит остаётся брендо-нейтральным (его дефолт исторический
``.skillery``); CLI инъектирует НОВЫЙ путь ``.skillery/skills.toml`` как
канонический (write) + legacy ``.skillery/skills.toml`` как fallback для
чтения старых проектов. Существующие репозитории с ``.skillery/skills.toml``
продолжают читаться; запись (add/save) идёт уже в ``.skillery``.
"""
from __future__ import annotations

from skillkit.project import (  # noqa: F401
    MANIFEST_REL,
    add,
    list_,
    load,
    manifest_path,
    remove,
    save,
)

# Бренд skillery конфигурируется ОДНИМ местом — core/_kit_config: оно зовёт
# skillkit.configure(SkillkitConfig(manifest_rel=.skillery, fallback=.skillery)).
# Импорт = сайд-эффект конфигурации. Раньше тут была hasattr-заглушка, т.к. кит
# на PyPI не имел провайдеров; s-skillkit>=0.2.0 несёт configure/SkillkitConfig.
from skillery_cli.core import _kit_config  # noqa: F401
