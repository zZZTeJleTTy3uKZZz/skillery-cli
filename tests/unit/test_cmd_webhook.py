"""``skillery webhook register|status|revoke`` — команды без сети и без gh/glab.

Хаб-клиент и провайдерский раннер подменяются целиком: проверяем сценарии
владельца (успех / повтор / провайдер недоступен / ошибка хаба) и главное —
что секрет webhook'а не утекает в машинный (json) вывод.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as output_module
from skillery_cli.commands import _common
from skillery_cli.commands import webhook as webhook_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import ApiError
from skillery_cli.core.webhook_setup import SKILL_QUERY_KEY

SECRET = "wh-secret-DO-NOT-LEAK"
# Приёмник хаба один на провайдера; адрес hook'а = приёмник + маркер навыка.
CALLBACK = "https://api.skillery.ru/webhooks/git/github"
HOOK_URL_17 = f"{CALLBACK}?{SKILL_QUERY_KEY}=17"
REPO = "https://github.com/acme/skills.git"


def _json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output_module, "_mode", "json")


def _text_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output_module, "_mode", "text")


def _stdout_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    assert lines, f"ожидали JSON в stdout, пусто: {out!r}"
    return json.loads(lines[-1])


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    register_result: dict[str, Any] | None = None,
    status_result: dict[str, Any] | None = None,
    register_error: Exception | None = None,
    skill: dict[str, Any] | None = None,
) -> MagicMock:
    """Подменяет ClientConfig/токены/HubClient — сеть не трогаем."""
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="owner@example.com",
        permissions=["skill.publish"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))

    client = MagicMock()
    calls: list[str] = []

    async def _get_skill(id_or_slug: str) -> dict[str, Any]:
        return skill or {"id": 17, "slug": id_or_slug, "repo_url": REPO}

    async def _register(skill_id: str) -> dict[str, Any]:
        calls.append(f"register:{skill_id}")
        if register_error is not None:
            raise register_error
        return dict(
            register_result
            or {
                "mode": "manual",
                "provider": "github",
                "url": CALLBACK,
                "secret": SECRET,
            }
        )

    async def _status(skill_id: str) -> dict[str, Any]:
        calls.append(f"status:{skill_id}")
        return dict(
            status_result
            or {"status": "manual", "provider": "github", "url": CALLBACK}
        )

    async def _delete(skill_id: str) -> None:
        calls.append(f"delete:{skill_id}")

    async def _close() -> None:
        return None

    client.get_skill = _get_skill
    client.register_skill_webhook = _register
    client.get_skill_webhook = _status
    client.delete_skill_webhook = _delete
    client.close = _close
    client.calls = calls
    monkeypatch.setattr(_common, "HubClient", lambda **kw: client)
    return client


def _runner(responses: list[tuple[int, str]]):
    box: list[dict[str, Any]] = []

    def _run(args, **kwargs):
        box.append({"args": list(args), "input": kwargs.get("input")})
        code, out = responses.pop(0) if responses else (0, "null")
        # Как настоящие gh/glab: на ненулевом коде текст ошибки идёт в stderr.
        return subprocess.CompletedProcess(
            args, code, stdout=out if code == 0 else "", stderr="" if code == 0 else out
        )

    _run.calls = box  # type: ignore[attr-defined]
    return _run


# --- (d) успешная регистрация ------------------------------------------------


def test_register_creates_provider_hook_and_reports_auto_update(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    client = _install_client(monkeypatch)
    runner = _runner([(0, "[]"), (0, json.dumps({"id": 101}))])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    payload = _stdout_json(capsys)
    assert payload["event"] == "webhook_register"
    assert payload["mode"] == "manual"  # хаб сам не смог — сделали мы
    assert payload["auto_update"] is True
    assert payload["provider_hook"]["action"] == "created"
    assert payload["provider_hook"]["hook_id"] == "101"
    assert client.calls == ["register:17"]


def test_register_auto_mode_does_not_touch_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Хаб создал hook сам (у него есть токен) — локальный gh не зовём."""
    _json_mode(monkeypatch)
    _install_client(
        monkeypatch,
        register_result={
            "mode": "auto",
            "provider": "github",
            "url": CALLBACK,
            "secret": None,
        },
    )
    runner = _runner([])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    payload = _stdout_json(capsys)
    assert payload["auto_update"] is True
    assert payload["provider_hook"]["action"] == "hub"
    assert runner.calls == []


# --- (d) повтор: идемпотентность --------------------------------------------


def test_register_repeat_updates_hook_without_duplicate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    _install_client(monkeypatch)
    existing = [{"id": 42, "config": {"url": HOOK_URL_17}}]
    runner = _runner([(0, json.dumps(existing)), (0, json.dumps({"id": 42}))])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    payload = _stdout_json(capsys)
    assert payload["provider_hook"]["action"] == "updated"
    assert payload["provider_hook"]["hook_id"] == "42"
    methods = [c["args"][c["args"].index("--method") + 1] for c in runner.calls]
    assert methods == ["GET", "PATCH"]  # второго hook'а не создаём


class FakeProvider:
    """Провайдер С СОСТОЯНИЕМ: помнит hook'и между вызовами, как настоящий.

    Нужен, чтобы проверять идемпотентность честно — двумя реальными прогонами
    команды, а не заранее подготовленным ответом.
    """

    def __init__(self) -> None:
        self.hooks: dict[str, dict[str, Any]] = {}
        self._next_id = 100

    def __call__(self, args, **kwargs):
        method = args[args.index("--method") + 1]
        path = args[-1] if args[-1] != "-" else args[-3]
        body = json.loads(kwargs["input"]) if kwargs.get("input") else None
        tail = path.split("/hooks", 1)[1].strip("/")
        out = "null"
        if method == "GET" and not tail:
            out = json.dumps(list(self.hooks.values()))
        elif method == "POST":
            hook_id = str(self._next_id)
            self._next_id += 1
            self.hooks[hook_id] = {"id": int(hook_id), **(body or {})}
            out = json.dumps({"id": int(hook_id)})
        elif method in ("PATCH", "PUT"):
            self.hooks[tail].update(body or {})
            out = json.dumps(self.hooks[tail])
        elif method == "DELETE":
            self.hooks.pop(tail, None)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def test_two_runs_leave_exactly_one_hook_with_current_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Два запуска подряд: у провайдера ОДИН hook, секрет — из последней выдачи.

    Хаб на каждом register выдаёт НОВЫЙ секрет и стирает прежнюю запись; если
    hook у провайдера не переписать тем же id, получим дубль со старым секретом
    (его доставки приёмник отобьёт 401 — и никто не заметит).
    """
    _json_mode(monkeypatch)
    provider = FakeProvider()
    monkeypatch.setattr(webhook_mod, "RUNNER", provider)

    for secret, expected in (("secret-run-1", "created"), ("secret-run-2", "updated")):
        # Хаб на каждом register выдаёт новый секрет — эмулируем это честно.
        _install_client(
            monkeypatch,
            register_result={
                "mode": "manual",
                "provider": "github",
                "url": CALLBACK,
                "secret": secret,
            },
        )
        webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)
        payload = _stdout_json(capsys)
        assert payload["provider_hook"]["action"] == expected

    assert len(provider.hooks) == 1
    hook = next(iter(provider.hooks.values()))
    assert hook["config"]["secret"] == "secret-run-2"
    assert hook["config"]["url"] == HOOK_URL_17


def test_monorepo_second_skill_gets_its_own_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Два навыка ОДНОГО репо → два hook'а: секрет соседа не затирается.

    Бэкенд ищет навыки по repo_url_key и проверяет подпись против секрета
    КАЖДОГО (fail-closed). Один общий hook хранит один секрет — значит все
    навыки репо, кроме последнего зарегистрированного, молча перестали бы
    синкаться.
    """
    _json_mode(monkeypatch)
    provider = FakeProvider()
    monkeypatch.setattr(webhook_mod, "RUNNER", provider)

    for skill_id, secret in ((17, "secret-A"), (18, "secret-B")):
        _install_client(
            monkeypatch,
            skill={"id": skill_id, "slug": "s", "repo_url": REPO},
            register_result={
                "mode": "manual",
                "provider": "github",
                "url": CALLBACK,
                "secret": secret,
            },
        )
        webhook_mod.cmd_webhook_register(slug="s", no_provider=False)
        assert _stdout_json(capsys)["provider_hook"]["action"] == "created"

    assert len(provider.hooks) == 2
    by_url = {h["config"]["url"]: h["config"]["secret"] for h in provider.hooks.values()}
    assert by_url == {
        f"{CALLBACK}?{SKILL_QUERY_KEY}=17": "secret-A",
        f"{CALLBACK}?{SKILL_QUERY_KEY}=18": "secret-B",
    }


# --- (d) провайдер недоступен → внятные ручные шаги --------------------------


def test_register_falls_back_to_manual_steps_when_tool_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    _install_client(monkeypatch)

    def _missing(args, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(webhook_mod, "RUNNER", _missing)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    payload = _stdout_json(capsys)
    assert payload["auto_update"] is False
    assert payload["provider_hook"]["reason"] == "tool_missing"
    steps = payload["manual_steps"]
    assert any(CALLBACK in s for s in steps)
    assert any("push" in s.lower() for s in steps)
    # Секрет для ручной вставки в json-режиме НЕ показываем.
    assert payload["secret_shown"] is False


def test_register_no_provider_flag_skips_local_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    _install_client(monkeypatch)
    runner = _runner([])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=True)

    payload = _stdout_json(capsys)
    assert payload["provider_hook"]["reason"] == "disabled_by_flag"
    assert runner.calls == []
    assert payload["manual_steps"]


def test_register_without_public_url_says_who_fixes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Хаб без публичного base URL: hook создавать некуда — говорим прямо."""
    _json_mode(monkeypatch)
    _install_client(
        monkeypatch,
        register_result={
            "mode": "manual",
            "provider": "github",
            "url": "",
            "secret": SECRET,
        },
    )
    runner = _runner([])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    payload = _stdout_json(capsys)
    assert payload["provider_hook"]["reason"] == "callback_url_missing"
    assert "админ" in payload["provider_hook"]["detail"]
    assert runner.calls == []


# --- (d) ошибка хаба ---------------------------------------------------------


def test_register_hub_error_exits_1_with_json_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    _install_client(
        monkeypatch,
        register_error=ApiError(422, "REPO_URL_MISSING", "У навыка не задан repo_url"),
    )
    runner = _runner([])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    with pytest.raises(SystemExit) as exc:
        webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # stdout остаётся чистым машинным каналом
    err = json.loads(captured.err.strip().splitlines()[-1])
    assert err["event"] == "error"
    assert err["code"] == "REPO_URL_MISSING"
    assert runner.calls == []  # к провайдеру не пошли


# --- (d) секрет не утекает ---------------------------------------------------


def test_secret_never_appears_in_json_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    _install_client(monkeypatch)

    def _missing(args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(webhook_mod, "RUNNER", _missing)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert "secret" not in payload
    assert payload["hub_secret_issued"] is True
    assert payload["secret_fingerprint"].startswith("sha256:")


def test_secret_shown_once_in_text_mode_only_when_manual(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Человеку секрет нужен для ручной вставки — печатаем один раз, с пометкой."""
    _text_mode(monkeypatch)
    _install_client(monkeypatch)

    def _missing(args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(webhook_mod, "RUNNER", _missing)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    out = capsys.readouterr().out
    assert out.count(SECRET) == 1
    assert "ОДИН раз" in out


def test_failed_update_of_existing_hook_is_reported_as_stale_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hook есть, обновить не вышло → у провайдера СТАРЫЙ секрет: говорим прямо.

    Хаб на register выдал новый секрет и стёр прежнюю запись — молчание здесь
    означало бы автосинк, который отбивается 401 и «просто не работает».
    """
    _json_mode(monkeypatch)
    _install_client(monkeypatch)
    existing = [{"id": 42, "config": {"url": HOOK_URL_17}}]
    # GET прошёл, PATCH упал с 403 — прав на изменение hook'а нет.
    runner = _runner([(0, json.dumps(existing)), (1, "HTTP 403: Forbidden")])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    payload = _stdout_json(capsys)
    hook = payload["provider_hook"]
    assert payload["auto_update"] is False
    assert hook["stale_secret"] is True
    assert hook["hook_id"] == "42"
    assert hook["reason"] == "tool_unauthorized"


def test_secret_with_markup_chars_is_printed_verbatim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Секрет печатаем БЕЗ rich-разметки: `[...]` внутри него не должно съесть.

    Искажённый при показе секрет человек вставит в настройки репо — и все
    доставки будут вечно отбиваться 401 без единого намёка на причину.
    """
    _text_mode(monkeypatch)
    tricky = "ab[bold]cd[/]ef"
    _install_client(
        monkeypatch,
        register_result={
            "mode": "manual",
            "provider": "github",
            "url": CALLBACK,
            "secret": tricky,
        },
    )

    def _missing(args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(webhook_mod, "RUNNER", _missing)

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    assert tricky in capsys.readouterr().out


def test_secret_not_printed_when_hook_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Авто-настройка удалась — показывать секрет человеку незачем."""
    _text_mode(monkeypatch)
    _install_client(monkeypatch)
    monkeypatch.setattr(
        webhook_mod, "RUNNER", _runner([(0, "[]"), (0, json.dumps({"id": 7}))])
    )

    webhook_mod.cmd_webhook_register(slug="my-skill", no_provider=False)

    out = capsys.readouterr().out
    assert SECRET not in out
    assert "Автообновления включены" in out


# --- status / revoke ---------------------------------------------------------


def test_status_reports_hub_state_without_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    _install_client(
        monkeypatch,
        status_result={"status": "registered", "provider": "github", "url": CALLBACK},
    )
    runner = _runner([])
    monkeypatch.setattr(webhook_mod, "RUNNER", runner)

    webhook_mod.cmd_webhook_status(slug="my-skill", probe=False)

    payload = _stdout_json(capsys)
    assert payload["status"] == "registered"
    assert payload["provider"] == "github"
    assert payload["url"] == CALLBACK
    assert "provider_hook" not in payload
    assert runner.calls == []


def test_status_probe_adds_last_delivery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    _install_client(monkeypatch)
    hooks = [
        {
            "id": 11,
            "active": True,
            "events": ["push"],
            "config": {"url": HOOK_URL_17},
            "last_response": {"code": 202},
        }
    ]
    deliveries = [{"delivered_at": "2026-07-19T10:00:00Z"}]
    monkeypatch.setattr(
        webhook_mod,
        "RUNNER",
        _runner([(0, json.dumps(hooks)), (0, json.dumps(deliveries))]),
    )

    webhook_mod.cmd_webhook_status(slug="my-skill", probe=True)

    payload = _stdout_json(capsys)
    hook = payload["provider_hook"]
    assert hook["found"] is True
    assert hook["last_delivery_at"] == "2026-07-19T10:00:00Z"
    assert hook["last_response_code"] == 202


def test_revoke_removes_hub_record_and_provider_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _json_mode(monkeypatch)
    client = _install_client(monkeypatch)
    hooks = [{"id": 8, "config": {"url": HOOK_URL_17}}]
    monkeypatch.setattr(webhook_mod, "RUNNER", _runner([(0, json.dumps(hooks)), (0, "")]))

    webhook_mod.cmd_webhook_revoke(slug="my-skill", no_provider=False)

    payload = _stdout_json(capsys)
    assert payload["hub_removed"] is True
    assert payload["provider_hook"]["action"] == "deleted"
    # Статус читаем ДО удаления — иначе callback-url уже не узнать.
    assert client.calls == ["status:17", "delete:17"]
