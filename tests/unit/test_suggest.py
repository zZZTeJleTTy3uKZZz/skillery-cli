"""Тесты discovery BASE matcher — чистые функции ``core/suggest.py``.

``normalize_query`` (запрос → значимые токены) и ``score_skill``
(meta + токены → (score, matched_on)) — на голых dict-фикстурах, без
CLI, стора и сети. Это объяснимый baseline-матчинг навыка по свободному
запросу пользователя.
"""
from __future__ import annotations

from typing import Any

from skills_hub_cli.core.suggest import normalize_query, score_skill


# ======================================================
#  normalize_query — запрос → значимые токены
# ======================================================
def test_normalize_lowercases_and_tokenizes() -> None:
    assert normalize_query("Stripe Payments") == ["stripe", "payments"]


def test_normalize_strips_punctuation() -> None:
    assert normalize_query("payments, stripe!") == ["payments", "stripe"]


def test_normalize_drops_ru_stopwords() -> None:
    # «нужно добавить» — стоп-слова, остаётся только значимое
    assert normalize_query("нужно добавить навык для stripe") == ["навык", "stripe"]


def test_normalize_drops_en_stopwords() -> None:
    assert normalize_query("a skill for the payments in stripe") == [
        "skill",
        "payments",
        "stripe",
    ]


def test_normalize_caps_at_five_tokens() -> None:
    out = normalize_query("alpha beta gamma delta epsilon zeta eta")
    assert len(out) == 5
    assert out == ["alpha", "beta", "gamma", "delta", "epsilon"]


def test_normalize_dedupes_preserving_order() -> None:
    assert normalize_query("stripe stripe payments stripe") == ["stripe", "payments"]


def test_normalize_empty_and_stopwords_only() -> None:
    assert normalize_query("") == []
    assert normalize_query("the a for in to") == []


def test_normalize_drops_short_tokens() -> None:
    # односимвольные огрызки пунктуации/чисел не считаем значимыми
    assert normalize_query("go x payments") == ["go", "payments"]


# ======================================================
#  score_skill — meta + токены → (score, matched_on)
# ======================================================
def _meta(
    *,
    slug: str = "",
    title: str = "",
    tags: tuple[str, ...] = (),
    description: str = "",
) -> dict[str, Any]:
    """Минимальный meta-dict в форме ``read_meta`` (slug/title + manifest)."""
    return {
        "slug": slug,
        "title": title,
        "manifest": {"tags": list(tags), "description": description},
    }


def test_score_exact_tag_weight() -> None:
    score, matched = score_skill(_meta(tags=("payments",)), ["payments"])
    assert score >= 4.0
    assert "tags:payments" in matched


def test_score_slug_substring_weight() -> None:
    score, matched = score_skill(_meta(slug="stripe-pay"), ["stripe"])
    assert score >= 3.0
    assert "slug:stripe" in matched


def test_score_title_substring_weight() -> None:
    score, matched = score_skill(_meta(title="Stripe Billing"), ["stripe"])
    assert score >= 2.0
    assert "title:stripe" in matched


def test_score_description_substring_weight() -> None:
    score, matched = score_skill(
        _meta(description="Integrate the Stripe API"), ["stripe"]
    )
    assert score >= 1.0
    assert "description:stripe" in matched


def test_score_tag_beats_description() -> None:
    tag_score, _ = score_skill(_meta(tags=("stripe",)), ["stripe"])
    desc_score, _ = score_skill(_meta(description="stripe"), ["stripe"])
    assert tag_score > desc_score


def test_score_no_match_is_zero() -> None:
    score, matched = score_skill(_meta(tags=("python",)), ["stripe"])
    assert score == 0.0
    assert matched == []


def test_score_empty_terms_is_zero() -> None:
    score, matched = score_skill(_meta(tags=("python",)), [])
    assert score == 0.0
    assert matched == []


def test_score_coverage_bonus_rewards_more_terms() -> None:
    """Покрытие двух разных terms ценнее, чем двойной хит одного."""
    two_terms, _ = score_skill(
        _meta(tags=("payments", "stripe")), ["payments", "stripe"]
    )
    one_term, _ = score_skill(_meta(tags=("payments",)), ["payments", "stripe"])
    assert two_terms > one_term


def test_score_accumulates_across_fields() -> None:
    """Один term бьёт в несколько полей — причины перечислены, очки растут."""
    score, matched = score_skill(
        _meta(slug="stripe", title="Stripe", tags=("stripe",)), ["stripe"]
    )
    # tag(+4) + slug(+3) + title(+2) минимум, плюс coverage-бонус
    assert score >= 9.0
    assert "tags:stripe" in matched
    assert "slug:stripe" in matched
    assert "title:stripe" in matched


def test_score_matched_on_human_readable_field_prefix() -> None:
    _score, matched = score_skill(
        _meta(tags=("payments",), description="card payments"), ["payments"]
    )
    # каждая причина — "<field>:<term>"
    for reason in matched:
        assert ":" in reason
        field = reason.split(":", 1)[0]
        assert field in {"tags", "slug", "title", "description"}


def test_score_local_boost_optional() -> None:
    """local_boost поднимает уже-локальный навык над равным удалённым."""
    boosted, _ = score_skill(
        _meta(tags=("python",)), ["python"], local_boost=1.5
    )
    plain, _ = score_skill(_meta(tags=("python",)), ["python"])
    assert boosted > plain


def test_score_case_insensitive() -> None:
    score, matched = score_skill(_meta(tags=("Python",)), ["python"])
    assert score >= 4.0
    assert "tags:python" in matched
