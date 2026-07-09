"""Хвосты P1 live-smoke (Phase E): 3 бага, найденные на живом dev-бэке.

1. transport._request не разворачивает FastAPI-обёртку {"detail": ...} →
   code=UNKNOWN, message=сырой JSON-блоб (EMAIL_TAKEN/PERMISSION_DENIED/
   LINK_UNUSABLE недоступны машинно).
2. commands/_common.run не json-aware: ApiError рендерится Rich-текстом в
   stdout под --json (все error-paths company/member/collection/onboard).
3. install --path не переносит tags/description из SKILL.md frontmatter в
   manifest меты → onboard не матчит локальные навыки по тегам.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from skillery_cli.core.transport import ApiError, HubClient


# ------------------------------------------------------------------
#  1. detail-unwrap в transport._request
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_api_error_unwraps_dict_detail() -> None:
    with respx.mock(base_url="http://t") as router:
        router.get("/companies/32").mock(
            return_value=Response(
                403,
                json={"detail": {"code": "PERMISSION_DENIED",
                                 "message": "Нет членства в этой компании"}},
            )
        )
        client = HubClient(base_url="http://t", access_token="x")
        try:
            with pytest.raises(ApiError) as ei:
                await client._request("GET", "/companies/32")
        finally:
            await client.close()
    assert ei.value.code == "PERMISSION_DENIED"
    assert "членства" in ei.value.message
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_api_error_unwraps_string_detail() -> None:
    with respx.mock(base_url="http://t") as router:
        router.get("/x").mock(
            return_value=Response(404, json={"detail": "Not Found"})
        )
        client = HubClient(base_url="http://t", access_token="x")
        try:
            with pytest.raises(ApiError) as ei:
                await client._request("GET", "/x")
        finally:
            await client.close()
    assert ei.value.message == "Not Found"
    assert "{" not in ei.value.message


@pytest.mark.asyncio
async def test_api_error_unwraps_validation_list_detail() -> None:
    """422 pydantic: detail=[{loc,msg,type},...] → code=VALIDATION, msg-сводка."""
    with respx.mock(base_url="http://t") as router:
        router.post("/auth/register").mock(
            return_value=Response(
                422,
                json={"detail": [{"loc": ["body", "password"],
                                  "msg": "String should have at least 8 characters",
                                  "type": "string_too_short"}]},
            )
        )
        client = HubClient(base_url="http://t", access_token="x")
        try:
            with pytest.raises(ApiError) as ei:
                await client._request("POST", "/auth/register", json={})
        finally:
            await client.close()
    assert ei.value.code == "VALIDATION"
    assert "password" in ei.value.message
    assert "{" not in ei.value.message  # не сырой JSON-блоб


@pytest.mark.asyncio
async def test_api_error_top_level_code_still_works() -> None:
    """Старый формат (code/message на верхнем уровне) не сломан."""
    with respx.mock(base_url="http://t") as router:
        router.get("/y").mock(
            return_value=Response(
                409, json={"code": "EMAIL_TAKEN", "message": "занято"}
            )
        )
        client = HubClient(base_url="http://t", access_token="x")
        try:
            with pytest.raises(ApiError) as ei:
                await client._request("GET", "/y")
        finally:
            await client.close()
    assert ei.value.code == "EMAIL_TAKEN"


# ------------------------------------------------------------------
#  2. _common.run — канон json-контракта (зеркало __main__._run)
# ------------------------------------------------------------------
def _json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli import output as out_mod

    monkeypatch.setattr(out_mod, "_mode", "json")


def test_common_run_api_error_json_mode(monkeypatch, capsys) -> None:
    from skillery_cli.commands import _common

    _json_mode(monkeypatch)

    async def boom() -> None:
        raise ApiError(status_code=403, code="PERMISSION_DENIED",
                       message="Нет членства", details={})

    with pytest.raises(SystemExit) as ei:
        _common.run(boom())
    assert ei.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout чист
    evt = json.loads(captured.err.strip().splitlines()[-1])
    assert evt["event"] == "error"
    assert evt["code"] == "PERMISSION_DENIED"


def test_common_run_api_error_text_mode_readable(monkeypatch, capsys) -> None:
    from skillery_cli import output as out_mod
    from skillery_cli.commands import _common

    monkeypatch.setattr(out_mod, "_mode", "text")

    async def boom() -> None:
        raise ApiError(status_code=404, code="NOT_FOUND",
                       message="нет такого", details={})

    with pytest.raises(SystemExit):
        _common.run(boom())
    captured = capsys.readouterr()
    assert "нет такого" in captured.out + captured.err


def test_common_run_typer_exit_passthrough(monkeypatch, capsys) -> None:
    """typer.Exit не должен превращаться в RUNTIME-событие (click наследует
    RuntimeError)."""
    import typer

    from skillery_cli.commands import _common

    _json_mode(monkeypatch)

    async def leave() -> None:
        raise typer.Exit(1)

    with pytest.raises((SystemExit, typer.Exit)):
        _common.run(leave())
    captured = capsys.readouterr()
    assert "RUNTIME" not in captured.err


# ------------------------------------------------------------------
#  3. local-path: frontmatter tags/description → manifest меты
# ------------------------------------------------------------------
def test_install_path_carries_tags_and_description(tmp_path: Path, monkeypatch) -> None:
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig
    from skillery_cli.core.agents import ClaudeCodeTarget
    from skillery_cli.core.installer import read_meta

    src = tmp_path / "fixture-skill"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: e2e-fix-skill\ndescription: Тестовый python-навык\n"
        "version: 1.0.0\ntags: [python, testing]\n---\n\n# Тело\n",
        encoding="utf-8",
    )

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    cfg = ClientConfig(store_dir=str(tmp_path / "store"), base_url="http://x")

    proj = tmp_path / "proj"
    proj.mkdir()
    asyncio.run(
        main_mod._install_local_source(
            cfg,
            source={"kind": "path", "slug": "e2e-fix-skill", "path": src},
            scope="project", project_path=proj, force=False,
            agent_target=target,
        )
    )

    meta = read_meta(tmp_path / "store" / "e2e-fix-skill")
    assert meta["manifest"].get("tags") == ["python", "testing"]
    assert "python" in (meta["manifest"].get("description") or "")
