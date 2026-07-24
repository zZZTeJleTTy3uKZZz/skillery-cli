"""#1102: транспорт push-обновлений — полный ответ очереди + рапорт device-task.

- ``fetch_device_queue_full`` отдаёт ОБА поля ответа: ``items`` (skill-очередь,
  как раньше) и ``device_tasks`` (обобщённые задачи устройства).
- ``fetch_device_queue`` остаётся тонкой обёрткой, возвращает ТОЛЬКО ``items``
  (обратная совместимость: skill-очередь не сломана).
- ``report_device_task`` шлёт терминальный статус на
  ``POST /devices/{cdid}/tasks/{task_id}/report``.
"""
from __future__ import annotations

import respx
from httpx import Response

from skillery_cli.core.transport import HubClient


async def test_full_response_carries_items_and_device_tasks() -> None:
    """Полный ответ несёт и skill-очередь, и device_tasks из ОДНОГО запроса."""
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/me/device-queue").mock(
            return_value=Response(
                200,
                json={
                    "items": [{"slug": "atlas", "desired_version": "1.0.0"}],
                    "device_tasks": [
                        {
                            "id": 7,
                            "task_type": "cli_upgrade",
                            "payload": {"target_version": "0.6.0"},
                            "status": "queued",
                        }
                    ],
                },
            )
        )
        client = HubClient(
            base_url="http://localhost:8000", access_token="t", timeout=35.0
        )
        try:
            resp = await client.fetch_device_queue_full(auto_update=True, wait=25)
        finally:
            await client.close()
    assert resp["items"] == [{"slug": "atlas", "desired_version": "1.0.0"}]
    assert resp["device_tasks"][0]["task_type"] == "cli_upgrade"
    assert resp["device_tasks"][0]["payload"]["target_version"] == "0.6.0"


async def test_full_response_defaults_device_tasks_on_old_backend() -> None:
    """Старый backend без device_tasks → пустой список (не падаем)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/me/device-queue").mock(
            return_value=Response(200, json={"items": [{"slug": "atlas"}]})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            resp = await client.fetch_device_queue_full()
        finally:
            await client.close()
    assert resp["items"] == [{"slug": "atlas"}]
    assert resp["device_tasks"] == []


async def test_thin_wrapper_still_returns_only_items() -> None:
    """``fetch_device_queue`` — совместимость: только items (device_tasks игнор)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/me/device-queue").mock(
            return_value=Response(
                200,
                json={
                    "items": [{"slug": "atlas"}],
                    "device_tasks": [{"id": 1, "task_type": "cli_upgrade"}],
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            items = await client.fetch_device_queue()
        finally:
            await client.close()
    assert items == [{"slug": "atlas"}]


async def test_report_device_task_posts_terminal_status() -> None:
    """Рапорт applied уходит на POST /devices/{cdid}/tasks/{id}/report."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/devices/dev-123/tasks/7/report").mock(
            return_value=Response(200, json={"status": "applied"})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.report_device_task(
                client_device_id="dev-123", task_id=7, status="applied"
            )
        finally:
            await client.close()
    assert route.called
    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body == {"status": "applied"}


async def test_report_device_task_failed_carries_trimmed_error() -> None:
    """Провал несёт причину (обрезаем до 500 символов, секреты не логируем)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/devices/dev-9/tasks/42/report").mock(
            return_value=Response(200, json={"status": "failed"})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.report_device_task(
                client_device_id="dev-9",
                task_id=42,
                status="failed",
                error="x" * 900,
            )
        finally:
            await client.close()
    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body["status"] == "failed"
    assert len(body["error"]) == 500
