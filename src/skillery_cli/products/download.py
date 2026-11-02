"""Скачивание артефактов и безопасная распаковка (ADR-0001, #5985).

Два обещания, которые нельзя нарушить:

1. **sha256 проверяется до распаковки и до запуска.** Непроверенный файл
   удаляется тут же; установщик со страницы релиза не запускается «на всякий
   случай», даже если очень хочется.
2. **распаковка не выходит за каталог назначения.** Абсолютные члены,
   ``..``, символические ссылки и устройства отвергаются до записи. Архив
   плагина приходит из сети, и доверять его именам нельзя.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from urllib.request import urlopen

DEFAULT_TIMEOUT = 120.0
MAX_BYTES = 512 * 1024 * 1024
CHUNK = 1024 * 1024


class ArtifactError(RuntimeError):
    """Артефакт не скачался, не распаковался или не прошёл проверку."""


class ChecksumError(ArtifactError):
    """sha256 не совпал (или его нет — проверять нечем)."""


class PathTraversalError(ArtifactError):
    """Архив пытается писать за пределы каталога назначения."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    """Проверить файл по sha256; пустая сумма — отказ, а не «доверяем»."""
    if not expected.strip():
        raise ChecksumError(
            f"у артефакта {path.name} нет sha256: проверять нечем, установка запрещена"
        )
    actual = sha256_file(path)
    if actual != expected.strip().lower():
        raise ChecksumError(
            f"sha256 не совпал у {path.name}: ожидалось {expected[:12]}…, "
            f"получено {actual[:12]}…"
        )


def download(url: str, dest: Path, *, timeout: float = DEFAULT_TIMEOUT) -> Path:
    """Скачать ``url`` (http/https/file) в ``dest``; вернуть путь."""
    if not url.strip():
        raise ArtifactError("у артефакта нет url: скачивать нечего")
    dest.parent.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    try:
        if parsed.scheme in ("", "file"):
            source = Path(parsed.path if parsed.scheme else url)
            _copy_limited(source=source, dest=dest)
        elif parsed.scheme in ("http", "https"):
            with (
                urlopen(url, timeout=timeout) as response,  # noqa: S310 — схема проверена
                dest.open("wb") as handle,
            ):
                total = 0
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ArtifactError(
                            f"артефакт больше {MAX_BYTES} байт — отказываемся"
                        )
                    handle.write(chunk)
        else:
            raise ArtifactError(f"схема {parsed.scheme!r} не поддерживается: {url}")
    except ArtifactError:
        raise
    except OSError as exc:
        raise ArtifactError(f"не скачался {url}: {exc}") from exc
    return dest


def _copy_limited(*, source: Path, dest: Path) -> None:
    if not source.is_file():
        raise ArtifactError(f"нет файла артефакта: {source}")
    if source.stat().st_size > MAX_BYTES:
        raise ArtifactError(f"артефакт больше {MAX_BYTES} байт — отказываемся")
    shutil.copyfile(source, dest)


def fetch_verified(
    url: str,
    expected_sha256: str,
    dest_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    downloader: Callable[..., Path] = download,
) -> Path:
    """Скачать и проверить: вернуть путь к ФАЙЛУ, который можно распаковывать."""
    if not expected_sha256.strip():
        raise ChecksumError(
            "у артефакта нет sha256: непроверенные байты не распаковываются и не запускаются"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name or "artifact.bin"
    dest = dest_dir / name
    result = downloader(url, dest, timeout=timeout)
    try:
        verify_sha256(result, expected_sha256)
    except ChecksumError:
        result.unlink(missing_ok=True)
        raise
    return result


def is_archive(path: Path) -> bool:
    lowered = path.name.lower()
    return lowered.endswith((".zip", ".tar.gz", ".tgz", ".tar"))


def safe_extract(archive: Path, dest: Path) -> Path:
    """Распаковать zip/tar.gz, не выпуская путей за ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if zipfile.is_zipfile(archive):
        _extract_zip(archive, root)
    elif tarfile.is_tarfile(archive):
        _extract_tar(archive, root)
    else:
        raise ArtifactError(f"неизвестный формат архива: {archive.name}")
    return dest


def _target(root: Path, member_name: str) -> Path:
    pure = PurePosixPath(member_name.replace("\\", "/"))
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise PathTraversalError(f"член архива выходит за каталог назначения: {member_name}")
    target = (root / pure).resolve()
    if target != root and root not in target.parents:
        raise PathTraversalError(f"член архива выходит за каталог назначения: {member_name}")
    return target


def _extract_zip(archive: Path, root: Path) -> None:
    with zipfile.ZipFile(archive) as handle:
        for info in handle.infolist():
            target = _target(root, info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise PathTraversalError(f"символическая ссылка в архиве: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out)


def _extract_tar(archive: Path, root: Path) -> None:
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            target = _target(root, member.name)
            if member.issym() or member.islnk():
                raise PathTraversalError(f"ссылка в архиве: {member.name}")
            if member.isdev() or member.isfifo():
                raise PathTraversalError(f"специальный файл в архиве: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise ArtifactError(f"не читается член архива: {member.name}")
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            if member.mode & 0o111:
                os.chmod(target, member.mode & 0o777)
