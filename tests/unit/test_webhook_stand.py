"""СТЕНД: реальный CLI (build_app) → реальный HubClient → HTTP-мок хаба.

Режим вывода настоящий CLI берёт пре-проходом по ``sys.argv`` при импорте
``__main__`` — здесь повторяем ровно это (``_parse_json_flag`` +
``init_output_mode``), иначе под CliRunner проверялся бы не тот путь.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
import respx
from typer.testing import CliRunner

from skillery_cli.commands import _common
from skillery_cli.commands import webhook as webhook_mod
from skillery_cli.config import ClientConfig

SECRET = "REAL-STAND-SECRET-9f2"
BASE = "http://hub.local"
CALLBACK = f"{BASE}/webhooks/git/github"


def _body_from_call(args: list[str], kwargs: dict) -> object:
    """Тело запроса из вызова провайдерского CLI — обе формы передачи.

    После #1266 тело уходит через ``--input <файл>``: форма ``--input -``
    отправляла ПУСТОЕ тело с кодом возврата 0, то есть вебхук молча не
    создавался.
    """
    if "--input" in args:
        source = args[args.index("--input") + 1]
        if source != "-":
            return json.loads(Path(source).read_text(encoding="utf-8"))
    raw = kwargs.get("input")
    return json.loads(raw) if raw else None


class FakeGh:
    def __init__(self) -> None:
        self.hooks: dict[str, dict] = {}
        self._n = 100
        self.argv_log: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.argv_log.append(list(args))
        method = args[args.index("--method") + 1]
        # Путь — ПО ПОЗИЦИИ (сразу за значением --method). После #1266 тело
        # уходит через `--input <файл>`, поэтому «последний аргумент» — это
        # путь к временному файлу, а не к API.
        path = args[args.index("--method") + 2]
        body = _body_from_call(args, kwargs)
        tail = path.split("/hooks", 1)[1].strip("/")
        out = "null"
        if method == "GET" and not tail:
            out = json.dumps(list(self.hooks.values()))
        elif method == "POST":
            hid = str(self._n)
            self._n += 1
            self.hooks[hid] = {"id": int(hid), **(body or {})}
            out = json.dumps({"id": int(hid)})
        elif method in ("PATCH", "PUT"):
            self.hooks[tail].update(body or {})
            out = json.dumps(self.hooks[tail])
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def _stand(monkeypatch: pytest.MonkeyPatch, *, argv: list[str]) -> None:
    """Собрать окружение реального запуска: config, токены, режим вывода."""
    import clikit.output as clikit_output

    from skillery_cli import output as output_module
    from skillery_cli.__main__ import _parse_json_flag
    from skillery_cli.output import init_output_mode

    # Режим вывода — глобальный: фиксируем через monkeypatch, чтобы стенд не
    # протекал в соседние тесты (teardown вернёт исходное значение).
    monkeypatch.setattr(output_module, "_mode", output_module._mode)
    monkeypatch.setattr(clikit_output, "_mode", clikit_output._mode)

    cfg = ClientConfig(
        base_url=BASE, user_email="o@e.com", permissions=["skill.publish"]
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("acc", "ref"))
    monkeypatch.setattr(sys, "argv", argv)
    init_output_mode(json_flag=_parse_json_flag(), config_format="text")


def _mock_hub() -> None:
    respx.get(f"{BASE}/skills/my-skill").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 17,
                "slug": "my-skill",
                "repo_url": "https://github.com/acme/skills.git",
            },
        )
    )
    respx.put(f"{BASE}/skills/17/webhook").mock(
        return_value=httpx.Response(
            200,
            json={
                "mode": "manual",
                "provider": "github",
                "url": CALLBACK,
                "secret": SECRET,
            },
        )
    )


@respx.mock
def test_stand_register_json_creates_hook_and_hides_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stand(monkeypatch, argv=["skillery", "--json", "webhook", "register", "my-skill"])
    gh = FakeGh()
    monkeypatch.setattr(webhook_mod, "RUNNER", gh)
    _mock_hub()

    from skillery_cli.__main__ import build_app

    result = CliRunner().invoke(
        build_app(), ["--json", "webhook", "register", "my-skill"]
    )
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    payload = json.loads(
        [ln for ln in result.stdout.strip().splitlines() if ln.strip()][-1]
    )
    assert payload["provider_hook"]["action"] == "created"
    assert payload["auto_update"] is True
    assert "secret" not in payload
    assert payload["secret_fingerprint"].startswith("sha256:")
    # Секрет ушёл провайдеру — но не в командную строку (её видно в ps).
    hook = next(iter(gh.hooks.values()))
    assert hook["config"]["secret"] == SECRET
    assert all(SECRET not in " ".join(a) for a in gh.argv_log)


@respx.mock
def test_stand_register_json_hides_secret_even_when_hook_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ветка, где секрет ВООБЩЕ печатается человеку: под --json — никогда."""
    _stand(monkeypatch, argv=["skillery", "--json", "webhook", "register", "my-skill"])

    def _no_gh(args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(webhook_mod, "RUNNER", _no_gh)
    _mock_hub()

    from skillery_cli.__main__ import build_app

    result = CliRunner().invoke(
        build_app(), ["--json", "webhook", "register", "my-skill"]
    )
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    payload = json.loads(
        [ln for ln in result.stdout.strip().splitlines() if ln.strip()][-1]
    )
    assert payload["auto_update"] is False
    assert payload["secret_shown"] is False
    assert payload["manual_steps"]
    assert all(SECRET not in step for step in payload["manual_steps"])


@respx.mock
def test_stand_register_text_shows_secret_once_for_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stand(monkeypatch, argv=["skillery", "webhook", "register", "my-skill"])

    def _no_gh(args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(webhook_mod, "RUNNER", _no_gh)
    _mock_hub()

    from skillery_cli.__main__ import build_app

    result = CliRunner().invoke(build_app(), ["webhook", "register", "my-skill"])
    assert result.exit_code == 0, result.output
    assert result.output.count(SECRET) == 1
    assert "ОДИН раз" in result.output
