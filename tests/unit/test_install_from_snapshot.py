"""Content-serving: CLI ставит из backend-снапшота, откатываясь на git clone."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import skillery_cli.__main__ as main_mod


class _FakeInstaller:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.snapshot_archive_existed: bool | None = None

    def install_from_snapshot(self, **kw: Any):
        self.calls.append("snapshot")
        # архив должен реально существовать на момент вызова
        self.snapshot_archive_existed = Path(kw["archive_path"]).is_file()
        return object()

    def install_from_path(self, **kw: Any):
        self.calls.append("path")
        self.local_src_existed = Path(kw["local_src"]).is_dir()
        return object()

    def install(self, request: Any = None, **kw: Any):
        # #1405: навык-в-подпапке ставится ЧЕРЕЗ install(InstallRequest) — только
        # так у меты появляется явная метка происхождения source_label="hub".
        if request is not None:
            self.calls.append("path")
            self.request = request
            self.local_src_existed = Path(request.source.local_src).is_dir()
            return object()
        self.calls.append("clone")
        return object()


class _FakeClient:
    def __init__(self, snap: bytes | None) -> None:
        self._snap = snap
        self.download_calls: list[tuple[str, str]] = []

    async def download_snapshot(self, ref: str, semver: str) -> bytes | None:
        self.download_calls.append((ref, semver))
        return self._snap


def _bundle(skill_path: str | None = None) -> dict:
    return {
        "commit_sha": "deadbeef",
        "manifest": {"version": "1.0.0", "files": []},
        "skill_path": skill_path,
        "skill_id": 5,
    }


@pytest.mark.asyncio
async def test_prefers_snapshot_when_available() -> None:
    installer = _FakeInstaller()
    client = _FakeClient(snap=b"\x1f\x8b\x08fake-tgz")
    res = await main_mod._materialize_from_bundle(
        installer, client, dep_slug="hello", dep_version="1.0.0",
        dep_bundle=_bundle(), dep_repo="https://git/x.git", dep_id=5,
        project_path=None, force=False,
    )
    assert res is not None
    assert installer.calls == ["snapshot"]  # снапшот, НЕ clone
    assert installer.snapshot_archive_existed is True
    assert client.download_calls == [("hello", "1.0.0")]


@pytest.mark.asyncio
async def test_falls_back_to_clone_when_no_snapshot() -> None:
    installer = _FakeInstaller()
    client = _FakeClient(snap=None)  # 404 → None
    await main_mod._materialize_from_bundle(
        installer, client, dep_slug="hello", dep_version="1.0.0",
        dep_bundle=_bundle(), dep_repo="https://git/x.git", dep_id=5,
        project_path=None, force=False,
    )
    assert installer.calls == ["clone"]
    assert client.download_calls == [("hello", "1.0.0")]  # пробовал снапшот


@pytest.mark.asyncio
async def test_subdir_skill_installs_from_snapshot_subdir(tmp_path) -> None:
    """Навык в подпапке (skill_path) ТЕПЕРЬ тоже ставится из снапшота: CLI сам
    извлекает подпапку и зовёт install_from_path. Это убирает git clone
    приватного репо (и «Authentication failed», и мигающее окно git-bash)."""
    import io
    import tarfile

    # реальный tar.gz репозитория с навыком в подпапке
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, body in [
            ("repo-abc/pyproject.toml", b"[project]"),
            ("repo-abc/skills/hello/SKILL.md", b"# hello"),
        ]:
            info = tarfile.TarInfo(name); info.size = len(body)
            t.addfile(info, io.BytesIO(body))

    installer = _FakeInstaller()
    client = _FakeClient(snap=buf.getvalue())
    await main_mod._materialize_from_bundle(
        installer, client, dep_slug="hello", dep_version="1.0.0",
        dep_bundle=_bundle(skill_path="skills/hello"), dep_repo="https://git/x.git",
        dep_id=5, project_path=None, force=False,
    )
    assert installer.calls == ["path"]         # из подпапки снапшота, НЕ clone
    assert installer.local_src_existed is True
    assert client.download_calls == [("hello", "1.0.0")]  # снапшот запрошен
    # #1405 КОРЕНЬ БАГА «установка из Хаба не становится главной»: подпапку
    # снапшота ставили как ЛОКАЛЬНУЮ папку, и кит писал в мету source=local-path
    # — навык навсегда выпадал из автообновления и выглядел локальным. Метка
    # происхождения обязана быть явной.
    assert installer.request.source_label == "hub"


@pytest.mark.asyncio
async def test_subdir_skill_falls_back_to_clone_on_bad_snapshot() -> None:
    """Снапшот есть, но подпапки в нём нет / архив битый → откат на git clone."""
    installer = _FakeInstaller()
    client = _FakeClient(snap=b"not-a-tarball")
    await main_mod._materialize_from_bundle(
        installer, client, dep_slug="hello", dep_version="1.0.0",
        dep_bundle=_bundle(skill_path="skills/hello"), dep_repo="https://git/x.git",
        dep_id=5, project_path=None, force=False,
    )
    assert installer.calls == ["clone"]
    assert client.download_calls == [("hello", "1.0.0")]  # снапшот пробовали


@pytest.mark.asyncio
async def test_broken_snapshot_falls_back_to_clone() -> None:
    """install_from_snapshot бросил (битый архив) → откат на clone."""
    client = _FakeClient(snap=b"corrupt")

    class _BrokenInstaller(_FakeInstaller):
        def install_from_snapshot(self, **kw: Any):
            self.calls.append("snapshot-fail")
            raise RuntimeError("bad archive")

    installer = _BrokenInstaller()
    await main_mod._materialize_from_bundle(
        installer, client, dep_slug="hello", dep_version="1.0.0",
        dep_bundle=_bundle(), dep_repo="https://git/x.git", dep_id=5,
        project_path=None, force=False,
    )
    assert installer.calls == ["snapshot-fail", "clone"]
