"""Загрузка артефактов и безопасная распаковка (#5985).

Скачанное проверяется по sha256 ДО распаковки и до запуска — это не
формальность: установщик запускается как программа, и «потом проверим» здесь
не бывает. Поэтому проверки идут на настоящих байтах: ``file://``, локальный
HTTP-сервер, zip и tar.gz с враждебными членами.
"""
from __future__ import annotations

import hashlib
import io
import tarfile
import threading
import zipfile
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from skillery_cli.products.download import (
    ChecksumError,
    PathTraversalError,
    download,
    fetch_verified,
    safe_extract,
    sha256_file,
    verify_sha256,
)
from skillery_cli.products.runner import CommandResult, SubprocessRunner


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Локальный HTTP-сервер над каталогом: тест качает «как с релиза»."""
    root = tmp_path / "www"
    root.mkdir()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", root
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _tar_bytes(entries: dict[str, bytes] | None = None, *, link: str = "") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in (entries or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if link:
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    return buffer.getvalue()


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"payload")
    assert sha256_file(path) == hashlib.sha256(b"payload").hexdigest()


def test_verify_sha256_accepts_match_and_rejects_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"payload")
    verify_sha256(path, hashlib.sha256(b"payload").hexdigest())
    with pytest.raises(ChecksumError, match="sha256"):
        verify_sha256(path, "0" * 64)


def test_verify_sha256_refuses_empty_checksum(tmp_path: Path) -> None:
    """Пустой sha256 — не «доверяем», а отказ: проверять нечем."""
    path = tmp_path / "blob.bin"
    path.write_bytes(b"payload")
    with pytest.raises(ChecksumError, match="sha256"):
        verify_sha256(path, "")


def test_download_file_url(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    dest = tmp_path / "dest.bin"
    download(source.as_uri(), dest)
    assert dest.read_bytes() == b"hello"


def test_download_http_url(served: tuple[str, Path], tmp_path: Path) -> None:
    base, root = served
    (root / "asset.bin").write_bytes(b"from-http")
    dest = tmp_path / "asset.bin"
    download(f"{base}/asset.bin", dest)
    assert dest.read_bytes() == b"from-http"


def test_fetch_verified_downloads_checks_and_removes_bad_file(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    base, root = served
    (root / "asset.bin").write_bytes(b"good")
    digest = hashlib.sha256(b"good").hexdigest()

    dest = fetch_verified(f"{base}/asset.bin", digest, tmp_path)
    assert dest.read_bytes() == b"good"

    (root / "bad.bin").write_bytes(b"evil")
    with pytest.raises(ChecksumError):
        fetch_verified(f"{base}/bad.bin", digest, tmp_path)
    assert not (tmp_path / "bad.bin").exists(), "непроверенный файл не оставляем"


def test_fetch_verified_refuses_missing_checksum(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    base, root = served
    (root / "asset.bin").write_bytes(b"good")
    with pytest.raises(ChecksumError):
        fetch_verified(f"{base}/asset.bin", "", tmp_path)


def test_safe_extract_zip(tmp_path: Path) -> None:
    archive = tmp_path / "plugin.zip"
    archive.write_bytes(_zip_bytes({"plugin/index.js": b"code", "plugin/package.json": b"{}"}))
    dest = tmp_path / "out"
    safe_extract(archive, dest)
    assert (dest / "plugin" / "index.js").read_bytes() == b"code"


def test_safe_extract_tar_gz(tmp_path: Path) -> None:
    archive = tmp_path / "plugin.tar.gz"
    archive.write_bytes(_tar_bytes({"plugin/index.js": b"code"}))
    dest = tmp_path / "out"
    safe_extract(archive, dest)
    assert (dest / "plugin" / "index.js").read_bytes() == b"code"


def test_safe_extract_rejects_zip_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    archive.write_bytes(_zip_bytes({"../escape.txt": b"x"}))
    with pytest.raises(PathTraversalError):
        safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_absolute_member(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    archive.write_bytes(_zip_bytes({"/etc/passwd": b"x"}))
    with pytest.raises(PathTraversalError):
        safe_extract(archive, tmp_path / "out")


def test_safe_extract_rejects_tar_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(_tar_bytes({"plugin/index.js": b"code"}, link="plugin/link"))
    with pytest.raises(PathTraversalError):
        safe_extract(archive, tmp_path / "out")


def test_subprocess_runner_returns_stdout_and_code() -> None:
    result = SubprocessRunner().run(
        ["python3", "-c", "import sys; print('ok'); sys.exit(3)"], timeout=30
    )
    assert isinstance(result, CommandResult)
    assert result.returncode == 3
    assert result.stdout.strip() == "ok"
