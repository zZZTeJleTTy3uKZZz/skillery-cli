"""Сравнение версий semver-подобных строк — одно на навыки и продукты.

Раньше ``_is_newer`` жил в ``__main__`` и был доступен только там. Синхронизации
продуктов (#5988) нужен тот же ответ на вопрос «плановая версия новее локальной?»,
а второй экземпляр сравнения разъехался бы с первым ровно на очередном
``1.2.0-rc1``. Поэтому логика переехала сюда, а ``__main__`` реэкспортирует
исторические имена ``_parse_version``/``_is_newer`` (на них ссылаются команды и
тесты).
"""
from __future__ import annotations

import re


def parse_version(raw: str) -> tuple[int, ...] | None:
    """Парсит semver-подобную строку в кортеж int-сегментов для сравнения.

    Толерантно к:
    - префиксу ``v``/``V`` (``v1.2.3`` → ``(1, 2, 3)``);
    - нечисловым хвостам/pre-release (``1.2.0-rc1`` → ``(1, 2, 0)`` — берём
      только ведущие числовые сегменты);
    Возвращает ``None``, если ни одного числового сегмента распарсить нельзя
    (вызывающий код тогда падает на строковое сравнение ``!=``).
    """
    s = raw.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    parts: list[int] = []
    for seg in s.split("."):
        m = re.match(r"\d+", seg.strip())
        if not m:
            break  # первый не-числовой сегмент обрывает разбор (хвост игнор)
        parts.append(int(m.group()))
    return tuple(parts) if parts else None


def is_newer(candidate: str, current: str) -> bool:
    """True, только если ``candidate`` СТРОГО новее ``current`` (semver-like).

    Баг B8: раньше апдейт гейтился ``bundle["version"] != current`` — downgrade
    воспринимался как апдейт. Сравниваем числовые сегменты; недостающие
    сегменты добиваются нулями (``1.2`` == ``1.2.0``). Если хотя бы одна из
    версий нераспарсиваема — fallback на строковое ``!=`` (не хуже прежнего
    поведения, но и не лучше — зато не маскирует баг для валидных версий).
    """
    cand = parse_version(candidate)
    cur = parse_version(current)
    if cand is None or cur is None:
        return candidate != current
    width = max(len(cand), len(cur))
    cand_p = cand + (0,) * (width - len(cand))
    cur_p = cur + (0,) * (width - len(cur))
    return cand_p > cur_p
