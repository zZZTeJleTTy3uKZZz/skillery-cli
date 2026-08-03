"""#1405 — «installed» и «list --installed» отвечают из ОДНОГО источника.

СИМПТОМ С МАШИНЫ ВЛАДЕЛЬЦА: ``skillery skill installed`` — 16 записей,
``skillery skill list --installed`` — 8. Читалось как «часть навыков
потерялась», хотя команды просто ходили разными сканерами (центральный стор
против каталогов агента) и по разным scope.

Инвариант: обе команды собирают выдачу одной функцией ``collect_installed``, а
scope подписан в самом выводе — расхождение по scope обязано выглядеть как
расхождение по scope, а не по числу установленных навыков.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import skillery_cli.__main__ as m
from skillery_cli.commands.installed import SCOPE_TITLES, cmd_installed
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "files": []}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Два навыка в сторе, один из них включён в проект."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()

    inst = SkillInstaller(target, store_dir=store)
    src = tmp_path / "src"
    for slug in ("alpha", "beta"):
        d = src / slug
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {slug}\nversion: 1.0.0\n---\n", encoding="utf-8"
        )
        inst.install(
            slug=slug, version="1.0.0", commit_sha="", repo_url=None,
            local_src=d, manifest=_MANIFEST, project=None,
        )
    inst.link_existing("alpha", project=project)

    cfg = ClientConfig(store_dir=str(store), default_project_dir=str(project))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(m, "get_target", lambda name: target, raising=False)
    monkeypatch.setattr(
        "skillery_cli.core.agents.get_target", lambda name: target, raising=False
    )
    from skillery_cli import output as out_mod

    monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, target, store, project


def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestSingleSourceOfTruth:
    def test_both_commands_return_identical_payload(self, env, capsys) -> None:
        """ГЛАВНОЕ: на один вопрос — один ответ (раньше было 16 против 8)."""
        cmd_installed(scope="all", project=None, agent=None)
        via_installed = _last_json(capsys)

        m.cmd_list(channel="published", installed=True, project=None, scope=None)
        via_list = _last_json(capsys)

        assert via_list == via_installed
        assert via_installed["count"] == len(via_installed["installed"])

    @pytest.mark.parametrize("scope", ["global", "project", "all"])
    def test_scope_agrees_between_commands(self, env, capsys, scope) -> None:
        """Разница по scope одинакова у обеих команд, а не «навыки потерялись»."""
        cmd_installed(scope=scope, project=None, agent=None)
        a = _last_json(capsys)
        m.cmd_list(channel="published", installed=True, project=None, scope=scope)
        b = _last_json(capsys)

        assert a == b
        assert a["scope"] == scope

    def test_scope_is_spelled_out_in_output(self, env, capsys) -> None:
        """Назначение выдачи видно в ней самой (заголовок про scope)."""
        cmd_installed(scope="global", project=None, agent=None)
        payload = _last_json(capsys)

        assert payload["scope_title"] == SCOPE_TITLES["global"]
        assert "global" in payload["scope_title"]

    def test_global_is_store_project_is_links(self, env, capsys) -> None:
        """scope=global — стор (оба навыка), project — только включённый."""
        cmd_installed(scope="global", project=None, agent=None)
        names = {i.get("slug") or i.get("name") for i in _last_json(capsys)["installed"]}
        assert names == {"alpha", "beta"}

        cmd_installed(scope="project", project=None, agent=None)
        proj = {i.get("slug") or i.get("ref") for i in _last_json(capsys)["installed"]}
        assert proj == {"alpha"}
