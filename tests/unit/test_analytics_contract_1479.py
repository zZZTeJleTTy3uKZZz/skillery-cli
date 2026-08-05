"""#1479: `skill analytics` читает ОБЕ формы ответа — конверт и прежнюю.

Переезд аналитики на конверт ``AnalyticsReport`` (#1451, backend-ветка
``feat/api-dead-routes-analytics``, спека ``docs/ops/analytics-contract.md``
ревизии ``7c398f2``) НЕ меняет путь ``GET /skills/{slug}/analytics`` — меняется
только тело. Контрактный тест путей такого не видит вовсе, а renderer, знающий
одну форму, молча печатает нули.

Поэтому тесты идут парой: конверт И текущий ответ `dev`. Толерантный парсер без
теста на старую форму — это способ сломать то, что работает сегодня.
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from skillery_cli.core.analytics_contract import is_envelope, normalize_report
from skillery_cli.core.transport import HubClient

_BASE = "http://testserver"

# Конверт — ровно та форма, что описана в §2 спеки.
ENVELOPE = {
    "window": {
        "since": "2026-07-06T00:00:00Z",
        "until": "2026-08-05T00:00:00Z",
        "days": 30,
        "preset": "30d",
    },
    "source": "postgres",
    "kpis": {
        "dau": 3.0, "wau": 9.0, "mau": 20.0,
        "runs_total": 128.0, "avg_ms": None, "p95_ms": None,
        "error_rate": 0.25, "ok_count": 96.0, "error_count": 32.0,
        "interrupted_count": 0.0,
    },
    "series": [
        {"metric": "installs", "points": [{"day": "2026-08-01", "count": 7}]},
        {"metric": "enables", "points": [{"day": "2026-08-01", "count": 2}]},
        {"metric": "runs", "points": []},
    ],
    "breakdowns": {
        "error_type": [{"key": "timeout", "count": 4}],
        "company": [{"key": "42", "label": "ООО Ромашка", "count": 9}],
    },
}

# Прежний ответ — то, что backend `dev` отдаёт СЕЙЧАС.
LEGACY = {
    "skill_slug": "vk",
    "period_from": "2026-07-06T00:00:00Z",
    "period_to": "2026-08-05T00:00:00Z",
    "source": "postgres",
    "etl_disabled": False,
    "installs_timeseries": [{"day": "2026-08-01", "count": 7}],
    "enables_timeseries": [{"day": "2026-08-01", "count": 2}],
    "runs_timeseries": [],
    "active_users": [
        {"period": "day", "distinct_users": 3},
        {"period": "week", "distinct_users": 9},
        {"period": "month", "distinct_users": 20},
    ],
    "run_stats": {
        "total": 128, "avg_ms": 12.5, "error_rate": 0.25, "p95_ms": 40.0,
        "ok_count": 96, "error_count": 32, "interrupted_count": 0,
        "error_types": [{"key": "timeout", "count": 4}],
        "versions": [{"key": "1.2.0", "count": 100}],
        "computed": True,
    },
    "top_companies": [
        {"company_id": "42", "company_name": "ООО Ромашка", "installs": 9}
    ],
}


class TestFormDiscrimination:
    def test_envelope_is_recognised(self) -> None:
        assert is_envelope(ENVELOPE)

    def test_legacy_is_not_mistaken_for_envelope(self) -> None:
        assert not is_envelope(LEGACY)

    def test_garbage_is_not_fatal(self) -> None:
        empty = normalize_report(None)
        assert empty["kpis"] == {} and empty["series"] == {}


class TestEnvelope:
    def test_kpis_and_series_are_read_as_is(self) -> None:
        got = normalize_report(ENVELOPE)
        assert got["kpis"]["runs_total"] == 128.0
        assert got["kpis"]["avg_ms"] is None, "None ≠ 0: «не посчитано» не ноль"
        assert got["series"]["installs"] == [{"day": "2026-08-01", "count": 7}]
        assert got["series"]["runs"] == []
        assert got["window"]["preset"] == "30d"
        assert got["breakdowns"]["company"][0]["label"] == "ООО Ромашка"


class TestLegacy:
    def test_same_numbers_come_out_of_the_old_shape(self) -> None:
        """Главное: обе формы дают ОДИН результат по общим полям."""
        old = normalize_report(LEGACY)
        new = normalize_report(ENVELOPE)
        for key in ("dau", "wau", "mau", "runs_total", "error_rate", "ok_count"):
            assert old["kpis"][key] == new["kpis"][key], key
        assert old["series"]["installs"] == new["series"]["installs"]
        assert old["breakdowns"]["error_type"] == new["breakdowns"]["error_type"]
        assert old["breakdowns"]["company"] == new["breakdowns"]["company"]

    def test_avg_ms_survives_the_old_shape(self) -> None:
        assert normalize_report(LEGACY)["kpis"]["avg_ms"] == 12.5

    def test_not_computed_becomes_none_not_zero(self) -> None:
        """``computed=false`` — «не считали», а не «0% ошибок»."""
        payload = json.loads(json.dumps(LEGACY))
        payload["run_stats"]["computed"] = False
        payload["run_stats"]["error_rate"] = 0.0
        kpis = normalize_report(payload)["kpis"]
        assert kpis["error_rate"] is None
        assert kpis["avg_ms"] is None
        assert kpis["runs_total"] == 128.0, "счётчики считаются всегда"

    def test_empty_answer_is_not_an_error(self) -> None:
        got = normalize_report({"skill_slug": "vk", "source": "none"})
        assert got["source"] == "none"
        assert got["kpis"] == {} and got["series"] == {}


class TestWindowParams:
    """Единое окно (§3 спеки): period / from / to / days уезжают в query."""

    @pytest.mark.asyncio
    async def test_all_window_forms_are_sent(self) -> None:
        with respx.mock(base_url=_BASE) as router:
            route = router.get("/skills/vk/analytics").mock(
                return_value=Response(200, json=ENVELOPE)
            )
            client = HubClient(base_url=_BASE)
            try:
                await client.get_skill_analytics("vk", period="30d")
                await client.get_skill_analytics("vk", days=7)
                await client.get_skill_analytics(
                    "vk", date_from="2026-08-01", date_to="2026-08-05"
                )
            finally:
                await client.close()
        queries = [dict(call.request.url.params) for call in route.calls]
        assert queries[0] == {"period": "30d"}
        assert queries[1] == {"days": "7"}
        assert queries[2] == {"from": "2026-08-01", "to": "2026-08-05"}
