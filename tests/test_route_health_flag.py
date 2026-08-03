"""Демон перестаёт молча глотать 404/405 на фоновом пути (#1441).

Проверяется ровно тот сценарий, из-за которого переименование роута прошло бы
CI зелёным: очередь заданий отвечает 404, демон не падает (это правильно) — но
теперь оставляет WARNING в ``daemon.log`` и видимый флаг в ``skillery status``.
"""
from __future__ import annotations

import json

import pytest

from skillery_cli.core import route_health
from skillery_cli.core.transport import ApiError


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLERY_HOME", str(tmp_path / ".skillery"))
    yield


def test_state_path_follows_skillery_home(tmp_path):
    assert route_health.state_path() == tmp_path / ".skillery" / "route_health.json"


def test_unknown_route_is_recorded_and_visible():
    assert route_health.unknown_routes() == []

    route_health.record_unknown_route("GET", "/me/device-queue?wait=25", 404)

    items = route_health.unknown_routes()
    assert len(items) == 1
    assert items[0]["route"] == "GET /me/device-queue", "query обязан отрезаться"
    assert items[0]["status"] == 404
    assert items[0]["count"] == 1


def test_repeat_bumps_counter_not_entries():
    route_health.record_unknown_route("GET", "/me/device-queue", 404)
    route_health.record_unknown_route("GET", "/me/device-queue", 404)
    items = route_health.unknown_routes()
    assert len(items) == 1
    assert items[0]["count"] == 2


def test_success_clears_the_flag():
    route_health.record_unknown_route("GET", "/me/device-queue", 404)
    assert route_health.unknown_routes()

    route_health.record_ok("GET", "/me/device-queue")

    assert route_health.unknown_routes() == [], (
        "после успешного ответа флаг обязан сниматься сам — иначе владелец "
        "видит вечное предупреждение об уже починенном"
    )


def test_only_contract_statuses_are_treated_as_rename():
    """500/401 — это НЕ переименование, они не должны поднимать флаг."""
    assert 404 in route_health.CONTRACT_STATUSES
    assert 405 in route_health.CONTRACT_STATUSES
    assert 500 not in route_health.CONTRACT_STATUSES
    assert 401 not in route_health.CONTRACT_STATUSES
    assert 403 not in route_health.CONTRACT_STATUSES


def test_broken_state_file_does_not_crash(tmp_path):
    path = route_health.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{битый json", encoding="utf-8")

    assert route_health.unknown_routes() == []
    route_health.record_unknown_route("GET", "/tasks", 405)
    assert len(route_health.unknown_routes()) == 1


def test_daemon_reconcile_raises_flag_on_404(monkeypatch, tmp_path):
    """Интеграция: очередь ответила 404 → демон не упал, но флаг поднят."""
    import asyncio

    from skillery_cli import __main__ as main

    class _Client:
        async def fetch_device_queue_full(self, **_: object) -> dict:
            raise ApiError(
                status_code=404, code="NOT_FOUND", message="Not Found"
            )

        async def close(self) -> None:
            return None

    cfg = main.ClientConfig.load()
    monkeypatch.setattr(cfg, "auto_update", False, raising=False)

    report = asyncio.run(
        main._reconcile_device_queue(
            cfg,
            "token",
            channel="stable",
            agent_target=None,
            client=_Client(),
        )
    )

    assert report == {"applied": [], "failed": [], "skipped": []}, (
        "демон обязан пережить 404 без падения"
    )
    items = route_health.unknown_routes()
    assert items and items[0]["status"] == 404, (
        "404 на очереди заданий обязан поднять видимый флаг, иначе тихий отказ "
        "неотличим от «нет заданий»"
    )
    assert items[0]["source"] == "daemon.reconcile"


def test_status_payload_carries_stale_routes(monkeypatch):
    """`skillery status` кладёт флаг в payload (и текст, и json)."""
    route_health.record_unknown_route("GET", "/me/device-queue", 404)

    from skillery_cli import __main__ as main

    captured: dict = {}

    def _emit(payload, text_renderer=None):
        captured.update(payload)
        if text_renderer is not None:
            text_renderer(payload)

    monkeypatch.setattr(main, "emit_data", _emit)
    main.cmd_status(project=None)

    assert captured.get("stale_routes"), (
        "статус обязан показывать расхождение контракта — это единственное "
        "место, куда владелец смотрит при «устройство молчит»"
    )
    assert captured["stale_routes"][0]["route"] == "GET /me/device-queue"


def test_state_file_is_valid_json_after_write():
    route_health.record_unknown_route("POST", "/telemetry/events", 405)
    data = json.loads(route_health.state_path().read_text(encoding="utf-8"))
    assert "POST /telemetry/events" in data["unknown_routes"]
