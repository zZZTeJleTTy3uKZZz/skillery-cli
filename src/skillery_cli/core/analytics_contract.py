"""Чтение аналитики двух форм: конверт ``AnalyticsReport`` и прежний ответ.

⚠️ **Толерантность здесь ВРЕМЕННАЯ.** Она живёт ровно до мёржа конверта
(#1451, backend-ветка ``feat/api-dead-routes-analytics``, спека
``docs/ops/analytics-contract.md`` на ревизии ``7c398f2``) в ``dev``. Как
только конверт станет единственной формой в ``dev``, ветку ``_from_legacy``
надо снести вместе с этим предупреждением — держать вечный «парсер на все
случаи» значит никогда не узнать, что старая форма умерла.

Зачем вообще: пути аналитики при переезде НЕ меняются, меняется только тело
ответа — то есть контрактный тест путей (#1441) этого не увидит **вообще**, а
CLI молча напечатает нули. Тихая неправда хуже падения: её не видно ни в логах,
ни в гейте.

Единая форма, к которой приводим обе (терминология — из спеки):

``{window, source, kpis, series: {metric: [{day, count}]}, breakdowns}``

``kpis`` — ``float | None``; ``None`` значит «не посчитано», и это **не ноль**
(для ``avg_ms``/``p95_ms``/``error_rate`` ноль — валидное значение). В прежней
форме тот же смысл нёс флаг ``run_stats.computed``, поэтому при
``computed=false`` эти три KPI переводим в ``None``, а не в ``0``.
"""
from __future__ import annotations

from typing import Any

#: Соответствие ``active_users[].period`` (прежняя форма) → ключ KPI конверта.
_ACTIVE_USER_KPI = {"day": "dau", "week": "wau", "month": "mau"}

#: Ряды навыка — те же три метрики, что перечислены в спеке (§5).
_LEGACY_SERIES = {
    "installs": "installs_timeseries",
    "enables": "enables_timeseries",
    "runs": "runs_timeseries",
}


def is_envelope(payload: Any) -> bool:
    """Ответ пришёл конвертом ``AnalyticsReport``, а не прежней формой.

    Признак — ключи конверта, а не отсутствие старых: конверт обязан нести
    ``kpis`` (dict) и ``series`` (list) всегда, даже пустыми.
    """
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("kpis"), dict)
        and isinstance(payload.get("series"), list)
    )


def normalize_report(payload: Any) -> dict[str, Any]:
    """Любая из двух форм → единый вид для рендера.

    Пустой словарь/список — законный ответ («нет данных»), не ошибка: клиент
    не должен различать отсутствие ключа и пустоту.
    """
    if not isinstance(payload, dict):
        return {"window": {}, "source": None, "kpis": {}, "series": {}, "breakdowns": {}}
    if is_envelope(payload):
        return _from_envelope(payload)
    return _from_legacy(payload)


def _points(raw: Any) -> list[dict[str, Any]]:
    return [p for p in (raw or []) if isinstance(p, dict)]


def _from_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    series: dict[str, list[dict[str, Any]]] = {}
    for item in payload.get("series") or []:
        if isinstance(item, dict) and item.get("metric"):
            series[str(item["metric"])] = _points(item.get("points"))
    breakdowns = {
        str(axis): [b for b in (buckets or []) if isinstance(b, dict)]
        for axis, buckets in (payload.get("breakdowns") or {}).items()
    }
    return {
        "window": payload.get("window") or {},
        "source": payload.get("source"),
        "kpis": dict(payload.get("kpis") or {}),
        "series": series,
        "breakdowns": breakdowns,
    }


def _from_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Прежний ответ ``/skills/{slug}/analytics`` (до #1451)."""
    stats = payload.get("run_stats")
    stats = stats if isinstance(stats, dict) else {}
    computed = bool(stats.get("computed"))

    kpis: dict[str, float | None] = {}
    if stats:
        kpis["runs_total"] = _num(stats.get("total"))
        kpis["ok_count"] = _num(stats.get("ok_count"))
        kpis["error_count"] = _num(stats.get("error_count"))
        kpis["interrupted_count"] = _num(stats.get("interrupted_count"))
        # `computed=false` ⇒ «не считали», а не «ноль ошибок» — в конверте это
        # выражается None'ом, поэтому и здесь None.
        for key, src in (
            ("avg_ms", "avg_ms"), ("p95_ms", "p95_ms"), ("error_rate", "error_rate")
        ):
            kpis[key] = _num(stats.get(src)) if computed else None
    for row in payload.get("active_users") or []:
        if not isinstance(row, dict):
            continue
        key = _ACTIVE_USER_KPI.get(str(row.get("period") or ""))
        if key:
            kpis[key] = _num(row.get("distinct_users"))

    series = {
        metric: _points(payload.get(field))
        for metric, field in _LEGACY_SERIES.items()
        if payload.get(field)
    }

    breakdowns: dict[str, list[dict[str, Any]]] = {}
    for axis, raw in (
        ("error_type", stats.get("error_types")),
        ("version", stats.get("versions")),
    ):
        buckets = [b for b in (raw or []) if isinstance(b, dict)]
        if buckets:
            breakdowns[axis] = buckets
    companies = [
        {
            "key": str(c.get("company_id") or ""),
            "label": c.get("company_name"),
            "count": int(c.get("installs") or 0),
        }
        for c in (payload.get("top_companies") or [])
        if isinstance(c, dict)
    ]
    if companies:
        breakdowns["company"] = companies

    return {
        "window": {
            "since": payload.get("period_from"),
            "until": payload.get("period_to"),
        },
        "source": payload.get("source"),
        "kpis": kpis,
        "series": series,
        "breakdowns": breakdowns,
    }


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
