"""Discovery BASE matcher — чистые функции ранжирования навыка по запросу.

Объяснимый baseline-матчинг свободного запроса пользователя
(«нужно добавить навык для stripe») против meta-формы навыка
(``slug``/``title`` + ``manifest.tags``/``description``):

1. :func:`normalize_query` — текст → ≤5 значимых токенов (lowercase,
   токенизация, отброс пунктуации и стоп-слов RU+EN, дедуп по порядку).
2. :func:`score_skill` — meta + токены → ``(score, matched_on)``: взвешенная
   сумма попаданий по полям (тег ≫ slug ≫ title ≫ description) + бонус за
   число покрытых уникальных terms; ``matched_on`` — человекочитаемые причины
   (``"tags:payments"``), это и есть объяснимость BASE-выдачи.

Развивает паттерн :mod:`skills_hub_cli.core.onboarding` (там сигнал ∈ tags |
⊂ slug | ⊂ description) до взвешенного скоринга по свободному запросу.
Чистые, без CLI/стора/сети — CLI-команда строится поверх отдельно.
"""
from __future__ import annotations

import re
from typing import Any

# Мини-список стоп-слов RU+EN: служебные слова запроса, не несущие смысла
# для матчинга навыка. Намеренно короткий — отсекаем шум, не домен.
_STOPWORDS: frozenset[str] = frozenset(
    {
        # EN
        "the", "a", "an", "for", "in", "to", "of", "on", "with", "and", "or",
        "how", "do", "i", "my", "is", "be", "add", "make", "need", "want",
        "use", "using", "please", "some", "this", "that",
        # RU
        "добавить", "добавь", "сделать", "сделай", "нужно", "надо", "как",
        "для", "в", "на", "с", "и", "или", "мне", "хочу", "нужен", "нужна",
        "это", "чтобы", "пожалуйста", "использовать",
    }
)

# Веса полей: точный тег ≫ подстрока slug ≫ подстрока title ≫ описание.
_WEIGHT_TAG = 4.0
_WEIGHT_SLUG = 3.0
_WEIGHT_TITLE = 2.0
_WEIGHT_DESCRIPTION = 1.0

# Бонус за каждый дополнительный покрытый уникальный term (поощряем широту
# покрытия запроса, а не повторные хиты одного слова).
_COVERAGE_BONUS = 1.0

# Минимальная длина значимого токена — режет огрызки пунктуации и одиночные
# символы (``x``), но пропускает короткие домены (``go``, ``ai``).
_MIN_TOKEN_LEN = 2

_MAX_TERMS = 5
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_query(text: str) -> list[str]:
    """Свободный запрос → ≤5 значимых токенов.

    Lowercase + токенизация по словам (буквы/цифры; пунктуация и ``_`` —
    разделители), отброс стоп-слов RU+EN и слишком коротких огрызков,
    дедуп с сохранением порядка, кап ``_MAX_TERMS``.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall((text or "").lower()):
        if len(token) < _MIN_TOKEN_LEN:
            continue
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= _MAX_TERMS:
            break
    return terms


def score_skill(
    meta: dict[str, Any],
    terms: list[str],
    *,
    local_boost: float = 0.0,
) -> tuple[float, list[str]]:
    """Оценивает навык против значимых ``terms`` запроса.

    Для каждого term начисляем веса по полям, в которые он попал: точный тег
    (``+4``), подстрока slug (``+3``), title (``+2``), description (``+1``) —
    поля складываются (term может бить сразу в несколько). Сверху — бонус за
    число уникальных покрытых terms (``_COVERAGE_BONUS`` за term) и
    опциональный ``local_boost`` (приоритет уже-локальному навыку).

    Возвращает ``(score, matched_on)``; ``matched_on`` — упорядоченные
    человекочитаемые причины ``"<field>:<term>"`` (объяснимость выдачи).
    Нет попаданий → ``(0.0, [])`` (``local_boost`` к пустому матчу не клеится).
    """
    if not terms:
        return 0.0, []

    manifest = meta.get("manifest") or {}
    tags = {str(t).lower() for t in (manifest.get("tags") or [])}
    description = str(manifest.get("description") or "").lower()
    slug = str(meta.get("slug") or "").lower()
    title = str(meta.get("title") or "").lower()

    # (field-name, вес, предикат попадания term в это поле)
    fields: tuple[tuple[str, float, Any], ...] = (
        ("tags", _WEIGHT_TAG, lambda t: t in tags),
        ("slug", _WEIGHT_SLUG, lambda t: bool(slug) and t in slug),
        ("title", _WEIGHT_TITLE, lambda t: bool(title) and t in title),
        (
            "description",
            _WEIGHT_DESCRIPTION,
            lambda t: bool(description) and t in description,
        ),
    )

    score = 0.0
    matched_on: list[str] = []
    covered: set[str] = set()
    for term in terms:
        for field_name, weight, hit in fields:
            if hit(term):
                score += weight
                matched_on.append(f"{field_name}:{term}")
                covered.add(term)

    if not matched_on:
        return 0.0, []

    score += _COVERAGE_BONUS * len(covered)
    score += local_boost
    return score, matched_on
