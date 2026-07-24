"""SK-2/A1: монорепо-навык ставит свой CLI-пакет из корня снапшота через uv tool.

Приватный CLI-навык — монорепо: SKILL.md в skills/<name>/, а пакет (pyproject+src)
в КОРНЕ. Раньше корень выкидывался → команда падала ModuleNotFoundError. Здесь
закреплено: снапшот-корень доезжает (.pkgsrc), _apply_tooling ставит пакет
`uv tool install` и убирает эту команду из манифеста для skillkit (без битого shim).
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

from skillery_cli import __main__ as m
from skillery_cli.core import cli_package_install as cpi

_PYPROJECT = '[project]\nname = "foo-cli"\nversion = "1.0.0"\n[project.scripts]\nfoo = "foo.cli:main"\n'


def _make_snapshot(tmp: Path) -> Path:
    """tar.gz монорепо: pyproject+src в корне, навык в skills/foo/."""
    files = {
        "repo-abc/pyproject.toml": _PYPROJECT,
        "repo-abc/src/foo/__init__.py": "",
        "repo-abc/src/foo/cli.py": "def main():\n    return 0\n",
        "repo-abc/skills/foo/SKILL.md": "# foo\n",
        "repo-abc/skills/foo/_skill_meta.toml": 'kind="tooling"\n',
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive = tmp / "snap.tar.gz"
    archive.write_bytes(buf.getvalue())
    return archive


def test_extract_snapshot_subdir_returns_sub_and_repo_root(tmp_path: Path) -> None:
    archive = _make_snapshot(tmp_path)
    into = tmp_path / "x"
    into.mkdir()
    res = m._extract_snapshot_subdir(archive, into, "skills/foo")
    assert res is not None
    sub, root = res
    assert (sub / "SKILL.md").is_file()
    assert sub.name == "foo" and sub.parent.name == "skills"
    # корень репо несёт pyproject пакета — раньше он терялся
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "foo" / "cli.py").is_file()


def test_persist_copies_repo_root_to_pkgsrc(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    store = tmp_path / "store" / "foo"
    store.mkdir(parents=True)
    result = type("R", (), {"store_dir": store})()

    m._persist_cli_package_source(root, result)

    assert (store / ".pkgsrc" / "pyproject.toml").is_file()
    assert cpi.read_pyproject_cli(store / ".pkgsrc")["name"] == "foo-cli"


def test_persist_noop_without_pyproject(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()  # нет pyproject → корень не пакет
    store = tmp_path / "store" / "foo"
    store.mkdir(parents=True)
    m._persist_cli_package_source(root, type("R", (), {"store_dir": store})())
    assert not (store / ".pkgsrc").exists()


def test_cli_package_root_prefers_pkgsrc(tmp_path: Path) -> None:
    store = tmp_path / "store" / "foo"
    (store / ".pkgsrc").mkdir(parents=True)
    (store / ".pkgsrc" / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    assert m._cli_package_root(store) == store / ".pkgsrc"


def test_apply_tooling_installs_package_and_strips_cli_for_kit(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "store" / "foo"
    (store / ".pkgsrc").mkdir(parents=True)
    (store / ".pkgsrc" / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    result = type("R", (), {"store_dir": store})()
    manifest = {
        "cli": [{"command_name": "foo", "entrypoint": "foo.cli:main"}],
        "mcp": [{"server_name": "srv"}],
        "runtime_dependencies": [],
    }

    seen: dict = {}

    def fake_install(pkg_root, *, command_names=None, **kw):
        seen["pkg_root"] = Path(pkg_root)
        seen["cmd_names"] = command_names
        return {"status": "installed", "package": "foo-cli",
                "commands": [{"name": "foo", "on_path": True, "ok": True}], "reason": ""}

    def fake_apply(result, *, agent_target, project, manifest):
        seen["kit_manifest"] = manifest
        return {"cli": [], "mcp": [], "deps": {}}

    monkeypatch.setattr(m.cli_package_install, "install_cli_package", fake_install)
    monkeypatch.setattr(m.tooling_install, "apply_tooling_artifacts", fake_apply)

    m._apply_tooling(result, manifest, agent_target=object(), project=None)

    # 1) поставили пакет из .pkgsrc, команду взяли из манифеста
    assert seen["pkg_root"] == store / ".pkgsrc"
    assert seen["cmd_names"] == ["foo"]
    # 2) skillkit получил манифест БЕЗ уже поставленной команды (без битого shim),
    #    но mcp/runtime_deps сохранены
    assert seen["kit_manifest"]["cli"] == []
    assert seen["kit_manifest"]["mcp"] == [{"server_name": "srv"}]


def test_apply_tooling_keeps_cli_for_kit_when_no_package(
    tmp_path: Path, monkeypatch
) -> None:
    """Публичный/atlas-путь: нет своего пакета в сторе → cli остаётся у skillkit."""
    store = tmp_path / "store" / "atlaslike"
    store.mkdir(parents=True)  # нет .pkgsrc/pyproject
    result = type("R", (), {"store_dir": store})()
    manifest = {"cli": [{"command_name": "atlas", "entrypoint": "atlas.cli:main"}]}

    installed_called = {"n": 0}
    seen: dict = {}
    monkeypatch.setattr(
        m.cli_package_install, "install_cli_package",
        lambda *a, **k: installed_called.__setitem__("n", installed_called["n"] + 1),
    )
    monkeypatch.setattr(
        m.tooling_install, "apply_tooling_artifacts",
        lambda result, **k: seen.setdefault("m", k["manifest"]) or {"cli": []},
    )

    m._apply_tooling(result, manifest, agent_target=object(), project=None)

    assert installed_called["n"] == 0, "нет своего пакета — uv tool install не зовём"
    assert seen["m"]["cli"] == manifest["cli"], "cli остаётся у skillkit (shim)"


def test_revert_uninstalls_cli_package(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store" / "foo"
    (store / ".pkgsrc").mkdir(parents=True)
    (store / ".pkgsrc" / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    monkeypatch.setattr(m, "read_meta", lambda d: {"manifest": {}})
    monkeypatch.setattr(m.tooling_install, "revert_tooling_artifacts", lambda *a, **k: None)
    uninstalled: list[str] = []
    monkeypatch.setattr(
        m.cli_package_install, "uninstall_cli_package",
        lambda name, **k: uninstalled.append(name) or True,
    )

    m._revert_tooling("foo", agent_target=object(), project=None, store_dir=store)

    assert uninstalled == ["foo-cli"]
