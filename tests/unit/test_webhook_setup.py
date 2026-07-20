"""webhook_setup — hook у провайдера через gh/glab БЕЗ сети и подпроцессов.

Раннер инъектируется, поэтому проверяем именно контракт: какие вызовы уходят,
идемпотентность (update вместо второго hook'а), адресность hook'а на НАВЫК
(монорепо: соседние навыки одного репо не затирают друг другу секрет),
деградация при недоступном провайдерском CLI и отсутствие секрета в argv.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from typing import Any

from skillery_cli.core.repo_connect import RepoSlug
from skillery_cli.core.webhook_setup import (
    SKILL_QUERY_KEY,
    delete_provider_hook,
    ensure_provider_hook,
    find_hook,
    hook_callback_url,
    manual_instructions,
    probe_provider_hook,
    secret_fingerprint,
)

GH = RepoSlug("github.com", "acme", "skills")
GL = RepoSlug("gitlab.com", "acme", "skills")
# Приёмник хаба — ОДИН на провайдера (адрес навыка добавляет маркер).
RECEIVER_GH = "https://api.skillery.ru/webhooks/git/github"
RECEIVER_GL = "https://api.skillery.ru/webhooks/git/gitlab"
SKILL = "17"
OTHER_SKILL = "18"
HOOK_GH = f"{RECEIVER_GH}?{SKILL_QUERY_KEY}={SKILL}"
HOOK_GL = f"{RECEIVER_GL}?{SKILL_QUERY_KEY}={SKILL}"


class FakeRunner:
    """Раннер-заглушка: отвечает по порядку, пишет все вызовы."""

    def __init__(self, responses: list[tuple[int, str]] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": list(args), "input": kwargs.get("input")})
        code, out = self._responses.pop(0) if self._responses else (0, "null")
        # stderr наполняем только на ненулевом коде (как настоящий gh/glab).
        return subprocess.CompletedProcess(
            args, code, stdout=out, stderr="" if code == 0 else out
        )


def _method(call: dict[str, Any]) -> str:
    args = call["args"]
    return args[args.index("--method") + 1]


def _path(call: dict[str, Any]) -> str:
    return call["args"][-1] if call["args"][-1] != "-" else call["args"][-3]


def test_github_hook_created_when_absent() -> None:
    runner = FakeRunner([(0, "[]"), (0, json.dumps({"id": 777}))])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s3cr3t",
        runner=runner,
    )
    assert out.action == "created"
    assert out.hook_id == "777"
    assert out.url == HOOK_GH
    assert _method(runner.calls[0]) == "GET"
    assert _method(runner.calls[1]) == "POST"
    # Тело — зеркало бэкенда: push + content_type json + secret.
    body = json.loads(runner.calls[1]["input"])
    assert body["events"] == ["push"]
    assert body["config"]["content_type"] == "json"
    assert body["config"]["url"] == HOOK_GH
    assert body["config"]["secret"] == "s3cr3t"


def test_repeat_updates_existing_hook_no_duplicate() -> None:
    """Повтор не плодит второй hook — обновляем найденный по URL."""
    existing = [{"id": 42, "config": {"url": HOOK_GH}}]
    runner = FakeRunner([(0, json.dumps(existing)), (0, json.dumps({"id": 42}))])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="new-secret",
        runner=runner,
    )
    assert out.action == "updated"
    assert out.hook_id == "42"
    assert _method(runner.calls[1]) == "PATCH"
    assert "hooks/42" in _path(runner.calls[1])
    # POST на создание не уходил — иначе был бы дубль.
    assert all(_method(c) != "POST" for c in runner.calls)
    # PATCH не шлёт name (GitHub его не принимает при обновлении).
    assert "name" not in json.loads(runner.calls[1]["input"])


def test_stale_hook_matched_by_skill_marker_and_updated() -> None:
    """Хаб сменил публичный адрес — свой hook узнаём по маркеру навыка."""
    stale = f"https://old.example/webhooks/git/github?{SKILL_QUERY_KEY}={SKILL}"
    existing = [{"id": 9, "config": {"url": stale}}]
    runner = FakeRunner([(0, json.dumps(existing)), (0, json.dumps({"id": 9}))])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s",
        runner=runner,
    )
    assert (out.action, out.hook_id) == ("updated", "9")
    # Адрес переписан на новый приёмник — старый висеть не остаётся.
    assert json.loads(runner.calls[1]["input"])["config"]["url"] == HOOK_GH


# --- монорепо: hook адресован НАВЫКУ, а не репозиторию ------------------------


def test_monorepo_sibling_skill_hook_is_not_hijacked() -> None:
    """Второй навык того же репо получает СВОЙ hook, а не перезапись соседнего.

    Приёмник у хаба один, а секрет — свой на каждый навык: перезапись hook'а
    соседа сменила бы секрет, и доставки по первому навыку молча отбивались бы
    401 (fail-closed на бэкенде), а автосинк тихо умер бы.
    """
    sibling = {
        "id": 42,
        "config": {"url": f"{RECEIVER_GH}?{SKILL_QUERY_KEY}={OTHER_SKILL}"},
    }
    runner = FakeRunner([(0, json.dumps([sibling])), (0, json.dumps({"id": 43}))])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="secret-of-17",
        runner=runner,
    )
    assert out.action == "created"  # НЕ updated — соседа не трогаем
    assert out.hook_id == "43"
    assert _method(runner.calls[1]) == "POST"
    assert json.loads(runner.calls[1]["input"])["config"]["url"] == HOOK_GH


def test_foreign_hook_on_same_receiver_is_not_touched() -> None:
    """Hook без маркера (создан самим хабом в auto-режиме) — чужой, не трогаем."""
    hub_made = [{"id": 1, "config": {"url": RECEIVER_GH}}]
    assert find_hook("github", hub_made, HOOK_GH, SKILL) is None


def test_delete_removes_only_own_skill_hook() -> None:
    hooks = [
        {"id": 42, "config": {"url": f"{RECEIVER_GH}?{SKILL_QUERY_KEY}={OTHER_SKILL}"}},
        {"id": 43, "config": {"url": HOOK_GH}},
    ]
    runner = FakeRunner([(0, json.dumps(hooks)), (0, "")])
    out = delete_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        runner=runner,
    )
    assert (out.action, out.hook_id) == ("deleted", "43")
    assert "hooks/43" in _path(runner.calls[1])


def test_delete_without_receiver_url_still_matches_by_marker() -> None:
    """У хаба не задан публичный адрес: свой hook всё равно узнаём по маркеру."""
    hooks = [
        {"id": 42, "config": {"url": f"{RECEIVER_GH}?{SKILL_QUERY_KEY}={OTHER_SKILL}"}},
        {"id": 43, "config": {"url": HOOK_GH}},
    ]
    runner = FakeRunner([(0, json.dumps(hooks)), (0, "")])
    out = delete_provider_hook(
        provider="github", repo=GH, callback_url="", skill_id=SKILL, runner=runner
    )
    assert (out.action, out.hook_id) == ("deleted", "43")


def test_probe_ignores_sibling_skill_hook() -> None:
    hooks = [{"id": 42, "config": {"url": f"{RECEIVER_GH}?{SKILL_QUERY_KEY}=99"}}]
    runner = FakeRunner([(0, json.dumps(hooks))])
    state = probe_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        runner=runner,
    )
    assert state.found is False


# --- GitLab ------------------------------------------------------------------


def test_gitlab_hook_created_with_token_and_tag_events() -> None:
    runner = FakeRunner([(0, "[]"), (0, json.dumps({"id": 5}))])
    out = ensure_provider_hook(
        provider="gitlab",
        repo=GL,
        callback_url=RECEIVER_GL,
        skill_id=SKILL,
        secret="glsecret",
        runner=runner,
    )
    assert out.action == "created"
    body = json.loads(runner.calls[1]["input"])
    assert body["token"] == "glsecret"
    assert body["url"] == HOOK_GL
    assert body["push_events"] is True and body["tag_push_events"] is True
    assert "projects/acme%2Fskills/hooks" in _path(runner.calls[1])
    assert runner.calls[1]["args"][0] == "glab"


def test_gitlab_update_uses_put() -> None:
    existing = [{"id": 3, "url": HOOK_GL}]
    runner = FakeRunner([(0, json.dumps(existing)), (0, json.dumps({"id": 3}))])
    out = ensure_provider_hook(
        provider="gitlab",
        repo=GL,
        callback_url=RECEIVER_GL,
        skill_id=SKILL,
        secret="s",
        runner=runner,
    )
    assert out.action == "updated"
    assert _method(runner.calls[1]) == "PUT"


# --- подпись: то, что кладём провайдеру, должно приниматься приёмником -------


def test_github_hook_body_matches_receiver_signature_contract() -> None:
    """Секрет уходит провайдеру БЕЗ преобразований — иначе HMAC не сойдётся.

    Приёмник считает ``X-Hub-Signature-256`` = HMAC-SHA256(секрет навыка, RAW
    тело). Провайдер подписывает тем, что лежит в ``config.secret``. Любая
    мутация секрета по дороге = молчаливый 401 на каждой доставке.
    """
    hub_secret = "hub-issued-secret-é"  # не-ASCII специально
    runner = FakeRunner([(0, "[]"), (0, json.dumps({"id": 1}))])
    ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret=hub_secret,
        runner=runner,
    )
    cfg = json.loads(runner.calls[1]["input"])["config"]
    assert cfg["secret"] == hub_secret
    # Провайдер подписал бы доставку так — приёмник ждёт ровно этого.
    raw_body = b'{"repository": {"clone_url": "https://github.com/acme/skills.git"}}'
    provider_sig = hmac.new(
        cfg["secret"].encode(), raw_body, hashlib.sha256
    ).hexdigest()
    expected_by_hub = hmac.new(
        hub_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(provider_sig, expected_by_hub)
    # content_type=json обязателен: приёмник парсит тело как JSON.
    assert cfg["content_type"] == "json"
    # Маркер навыка живёт в query — путь приёмника не искажён.
    assert cfg["url"].split("?")[0] == RECEIVER_GH


def test_gitlab_hook_token_is_the_hub_secret_verbatim() -> None:
    """GitLab шлёт ``X-Gitlab-Token`` == ``token`` hook'а; приёмник сверяет его
    с секретом навыка побайтово."""
    hub_secret = "hub-issued-secret"
    runner = FakeRunner([(0, "[]"), (0, json.dumps({"id": 1}))])
    ensure_provider_hook(
        provider="gitlab",
        repo=GL,
        callback_url=RECEIVER_GL,
        skill_id=SKILL,
        secret=hub_secret,
        runner=runner,
    )
    body = json.loads(runner.calls[1]["input"])
    assert hmac.compare_digest(body["token"], hub_secret)
    assert body["url"].split("?")[0] == RECEIVER_GL


# --- гигиена секрета ---------------------------------------------------------


def test_secret_never_in_argv() -> None:
    """Секрет уходит только через stdin — иначе он виден в списке процессов."""
    runner = FakeRunner([(0, "[]"), (0, json.dumps({"id": 1}))])
    ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="TOP-SECRET-VALUE",
        runner=runner,
    )
    for call in runner.calls:
        assert "TOP-SECRET-VALUE" not in " ".join(call["args"])
    assert "TOP-SECRET-VALUE" in (runner.calls[1]["input"] or "")


def test_provider_stderr_is_not_echoed_outward() -> None:
    """Вывод gh/glab наружу не пересказываем: в нём может быть эхо запроса."""

    def _runner(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr='HTTP 422: {"config":{"secret":"TOP-SECRET-VALUE"}}',
        )

    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="TOP-SECRET-VALUE",
        runner=_runner,
    )
    assert out.action == "skipped"
    assert "TOP-SECRET-VALUE" not in (out.detail or "")
    assert "TOP-SECRET-VALUE" not in (out.reason or "")


# --- деградация вместо падения -----------------------------------------------


def test_tool_missing_is_skipped_not_raise() -> None:
    def _runner(args, **kwargs):
        raise FileNotFoundError("gh not found")

    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s",
        runner=_runner,
    )
    assert out.action == "skipped"
    assert out.reason == "tool_missing"
    assert "gh" in (out.detail or "")


def test_unauthorized_and_not_found_are_distinguished() -> None:
    """«Не залогинен» и «нет такого репо» — разные причины и разные советы."""
    unauth = FakeRunner([(1, "gh: HTTP 401: Bad credentials")])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s",
        runner=unauth,
    )
    assert out.reason == "tool_unauthorized"
    assert "auth status" in (out.detail or "")

    missing = FakeRunner([(1, "gh: Not Found (HTTP 404)")])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s",
        runner=missing,
    )
    assert out.reason == "repo_not_found"
    assert "repo_url" in (out.detail or "")


def test_network_failure_is_generic_tool_failed() -> None:
    runner = FakeRunner([(1, "dial tcp: lookup api.github.com: no such host")])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s",
        runner=runner,
    )
    assert (out.action, out.reason) == ("skipped", "tool_failed")


def test_timeout_is_skipped_not_raise() -> None:
    def _runner(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=30)

    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s",
        runner=_runner,
    )
    assert (out.action, out.reason) == ("skipped", "tool_failed")


def test_bad_json_is_skipped() -> None:
    runner = FakeRunner([(0, "not-json")])
    out = ensure_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        secret="s",
        runner=runner,
    )
    assert (out.action, out.reason) == ("skipped", "bad_response")


def test_unsupported_provider_and_empty_callback() -> None:
    runner = FakeRunner([])
    assert (
        ensure_provider_hook(
            provider="bitbucket",
            repo=GH,
            callback_url=RECEIVER_GH,
            skill_id=SKILL,
            secret="s",
            runner=runner,
        ).reason
        == "provider_unsupported"
    )
    assert (
        ensure_provider_hook(
            provider="github",
            repo=GH,
            callback_url="",
            skill_id=SKILL,
            secret="s",
            runner=runner,
        ).reason
        == "callback_url_missing"
    )
    assert runner.calls == []  # ни одного вызова наружу


# --- probe / delete ----------------------------------------------------------


def test_probe_reports_last_delivery_github() -> None:
    hooks = [
        {
            "id": 11,
            "active": True,
            "events": ["push"],
            "config": {"url": HOOK_GH},
            "last_response": {"code": 202},
        }
    ]
    deliveries = [{"delivered_at": "2026-07-19T10:00:00Z"}]
    runner = FakeRunner([(0, json.dumps(hooks)), (0, json.dumps(deliveries))])
    state = probe_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        runner=runner,
    )
    assert state.found is True
    assert state.hook_id == "11"
    assert state.last_delivery_at == "2026-07-19T10:00:00Z"
    assert state.last_response_code == 202
    assert state.events == ("push",)


def test_probe_not_found_and_tool_missing() -> None:
    runner = FakeRunner([(0, "[]")])
    assert (
        probe_provider_hook(
            provider="github",
            repo=GH,
            callback_url=RECEIVER_GH,
            skill_id=SKILL,
            runner=runner,
        ).found
        is False
    )

    def _missing(args, **kwargs):
        raise FileNotFoundError

    state = probe_provider_hook(
        provider="gitlab",
        repo=GL,
        callback_url=RECEIVER_GL,
        skill_id=SKILL,
        runner=_missing,
    )
    assert state.found is False and state.reason == "tool_missing"


def test_delete_provider_hook_idempotent() -> None:
    hooks = [{"id": 8, "config": {"url": HOOK_GH}}]
    runner = FakeRunner([(0, json.dumps(hooks)), (0, "")])
    out = delete_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        runner=runner,
    )
    assert (out.action, out.hook_id) == ("deleted", "8")
    assert _method(runner.calls[1]) == "DELETE"

    # Уже нет — тоже успех отзыва.
    out2 = delete_provider_hook(
        provider="github",
        repo=GH,
        callback_url=RECEIVER_GH,
        skill_id=SKILL,
        runner=FakeRunner([(0, "[]")]),
    )
    assert (out2.action, out2.reason) == ("deleted", "not_found")


# --- мелочи ------------------------------------------------------------------


def test_find_hook_ignores_foreign_hooks() -> None:
    hooks = [{"id": 1, "config": {"url": "https://ci.example/other"}}]
    assert find_hook("github", hooks, HOOK_GH, SKILL) is None
    assert find_hook("github", None, HOOK_GH, SKILL) is None


def test_hook_callback_url_appends_marker_safely() -> None:
    assert hook_callback_url(RECEIVER_GH, 17) == f"{RECEIVER_GH}?{SKILL_QUERY_KEY}=17"
    # У приёмника уже есть query — добавляем через &, а не ломаем URL.
    assert (
        hook_callback_url(f"{RECEIVER_GH}?a=1", 17)
        == f"{RECEIVER_GH}?a=1&{SKILL_QUERY_KEY}=17"
    )
    assert hook_callback_url("", 17) == ""


def test_secret_fingerprint_does_not_reveal_secret() -> None:
    fp = secret_fingerprint("super-secret")
    assert fp is not None
    assert fp.startswith("sha256:") and len(fp) == len("sha256:") + 12
    assert "super-secret" not in fp
    assert secret_fingerprint(None) is None


def test_manual_instructions_have_marked_url_but_no_secret() -> None:
    for provider, receiver in (("github", RECEIVER_GH), ("gitlab", RECEIVER_GL)):
        steps = manual_instructions(provider, receiver, SKILL)
        marked = hook_callback_url(receiver, SKILL)
        assert any(marked in s for s in steps)
        assert all("s3cr3t" not in s for s in steps)
