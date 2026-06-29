"""Глобальная настройка client-тестов.

CLI всегда конфигурирует кит ``skillkit`` под бренд **skillery** при старте
(``core/_kit_config`` → ``skillkit.configure``). Делаем то же для тестов, чтобы
брендо-зависимое поведение (сообщения «не управляется skillery», rc-маркер
PATH, проектный манифест ``.skillery/skills.toml`` + legacy ``.skills-hub``
fallback) было детерминированным в т.ч. при запуске одного теста в изоляции
(иначе бренд зависит от того, импортнулся ли транзитивно один из шимов).
"""
from __future__ import annotations

# Сайд-эффект импорта: skillkit.configure(SkillkitConfig(app_name="skillery", ...)).
import skills_hub_cli.core._kit_config  # noqa: F401
