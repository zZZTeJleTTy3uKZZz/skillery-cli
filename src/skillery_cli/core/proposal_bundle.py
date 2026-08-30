"""Бандл предложения-с-кодом: сборка, слепок базы, список изменений (#2455).

Отдельный модуль, а не тело команды: ровно эти три операции — «собрать архив»,
«посчитать слепок», «сравнить с базой» — обязаны быть проверяемы без сети и без
файловой системы пользователя. Команда поверх них только разговаривает с
человеком.

────────────────────────────────────────────────────────────────────────────
ПОЧЕМУ АРХИВ СОБИРАЕТСЯ ДЕТЕРМИНИРОВАННО

``tar.gz`` по умолчанию тащит в заголовки время файла, uid/gid и порядок обхода
каталога. Два вызова подряд на неизменившейся папке дали бы РАЗНЫЕ байты и
разный ``bundle_digest`` — а на него смотрят и лимиты, и сверка «в хранилище
лежит то, что приняли». Поэтому имена сортируются, время обнуляется, владелец
обезличивается: одинаковое содержимое ⇒ одинаковый архив.

────────────────────────────────────────────────────────────────────────────
ПОЧЕМУ СЛЕПОК СЧИТАЕТСЯ ПО ДЕРЕВУ, А НЕ ПО БАЙТАМ АРХИВА

``base_digest`` — это пин базы: «я правил вот эту версию». Считать его от байт
скачанного архива значило бы привязаться к тому, КАК хаб упаковал версию
(порядок членов, уровень сжатия), а не к тому, ЧТО в ней лежит. Пересжатие того
же содержимого сменило бы пин на ровном месте. Слепок дерева
(``путь:sha256(содержимое)``, отсортированно) зависит только от содержимого — и
его же можно посчитать локально по распакованной копии.
"""
from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

#: Папки, которые в бандл не едут. Тот же список, что у сканера секретов
#: (``core.secret_scan._IGNORE_DIRS``) — импортируем его, а не копируем:
#: разъехавшись, копии дали бы «просканировали одно, отправили другое».
from skillery_cli.core.secret_scan import _IGNORE_DIRS as IGNORE_DIRS

#: Файлы, которые в бандл не едут, даже лежа в корне навыка.
#:
#: ``_skill_meta.json`` пишет УСТАНОВЩИК на этой машине: там агент, scope и
#: абсолютный путь проекта. Владельцу навыка это не нужно (у него своя
#: установка), а вот путь вида ``C:/Users/<имя>/…`` уехал бы к постороннему
#: вместе с предложением. Плюс в слепке версии этого файла нет по построению,
#: и без исключения он в КАЖДОМ предложении показывался бы «добавленным».
IGNORE_FILES = frozenset({"_skill_meta.json"})

#: Лимиты бандла — зеркало ``domain/skill_proposal/bundle.py`` бэкенда.
#: Проверяем ЗДЕСЬ, чтобы отказ стоил чтение папки, а не заливку пяти мегабайт
#: по мобильному интернету с 413 в конце.
MAX_BUNDLE_BYTES = 5 * 1024 * 1024
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_BUNDLE_FILES = 2000
MAX_BUNDLE_DEPTH = 20


class ProposalBundleError(ValueError):
    """Папку навыка нельзя отправить как предложение."""


def collect_files(skill_dir: Path | str) -> dict[str, bytes]:
    """Файлы навыка для бандла: ``{относительный путь: содержимое}``.

    Служебные папки (``_local``, ``.git``, кеши) отбрасываются: в них лежит
    локальное состояние конкретной машины, и владельцу навыка оно не только
    не нужно — оно ещё и рискует нести чужие данные.
    """
    root = Path(skill_dir)
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if any(part in IGNORE_DIRS for part in parts):
            continue
        if len(parts) == 1 and parts[0] in IGNORE_FILES:
            continue
        out["/".join(parts)] = path.read_bytes()
    return out


def check_limits(files: dict[str, bytes]) -> None:
    """Отказать ДО сети, если бандл заведомо не пройдёт на той стороне."""
    if not files:
        raise ProposalBundleError(
            "В папке навыка нет ни одного файла для отправки"
        )
    if len(files) > MAX_BUNDLE_FILES:
        raise ProposalBundleError(
            f"Файлов {len(files)}, потолок {MAX_BUNDLE_FILES}. "
            "Скорее всего в папку попал кеш или виртуальное окружение."
        )
    unpacked = sum(len(data) for data in files.values())
    if unpacked > MAX_UNPACKED_BYTES:
        raise ProposalBundleError(
            f"Содержимое весит {unpacked} байт, потолок {MAX_UNPACKED_BYTES}. "
            "Навыку не место бинарникам — вынесите их из папки."
        )
    for path in files:
        if len(path.split("/")) > MAX_BUNDLE_DEPTH:
            raise ProposalBundleError(
                f"Глубина вложенности пути больше {MAX_BUNDLE_DEPTH}: {path}"
            )


def make_bundle(files: dict[str, bytes]) -> bytes:
    """Детерминированный ``tar.gz`` из набора файлов.

    Время, владелец и порядок фиксированы — см. докстринг модуля: иначе
    одинаковое содержимое давало бы разный ``bundle_digest``.
    """
    buffer = io.BytesIO()
    # mtime=0 у самого gzip-заголовка: иначе метка времени архива попадала бы
    # в байты и ломала детерминизм ровно так же, как времена файлов.
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        gz = tar.fileobj  # type: ignore[attr-defined]
        if hasattr(gz, "mtime"):
            gz.mtime = 0  # type: ignore[union-attr]
        for path in sorted(files):
            data = files[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def tree_digest(files: dict[str, bytes]) -> str:
    """Слепок СОДЕРЖИМОГО дерева: ``sha256:…``.

    Зависит только от путей и содержимого — не от упаковки. Именно этим он и
    годится в ``base_digest``: пересжатие той же версии не меняет пин.
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[path]).digest())
    return "sha256:" + digest.hexdigest()


def unpack_snapshot(data: bytes) -> dict[str, bytes]:
    """Слепок версии с хаба → ``{относительный путь: содержимое}``.

    Общий корневой каталог архива срезается: хаб пакует версию папкой с именем
    навыка, а сравнивать надо содержимое с содержимым — иначе КАЖДЫЙ файл
    выглядел бы переименованным.
    """
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.replace("\\", "/").lstrip("./")
            handle = tar.extractfile(member)
            if handle is None:
                continue
            out[name] = handle.read()
    prefix = _common_root(out)
    if prefix:
        out = {
            path[len(prefix) + 1 :]: data
            for path, data in out.items()
            if path.startswith(prefix + "/")
        }
    return out


def _common_root(files: dict[str, bytes]) -> str:
    """Общий корневой каталог всех путей — либо пустая строка."""
    roots = {path.split("/", 1)[0] for path in files if "/" in path}
    if len(roots) != 1:
        return ""
    root = roots.pop()
    # Файл в корне архива рядом с папкой ⇒ общего корня нет.
    if any("/" not in path for path in files):
        return ""
    return root


@dataclass(frozen=True, slots=True)
class BundleChanges:
    """Что именно уедет владельцу навыка."""

    added: tuple[str, ...]
    modified: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.added or self.modified or self.removed)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.added, *self.modified, *self.removed}))


def diff_files(
    base: dict[str, bytes], current: dict[str, bytes]
) -> BundleChanges:
    """Сравнить базу и локальную копию по СОДЕРЖИМОМУ.

    Сравнение байтовое, а не по времени изменения: время правится
    редактором, синхронизацией и просто копированием папки, и «изменённым»
    оказался бы весь навык.

    Удалённые файлы показываются наравне с добавленными: предложение
    применяется как целое, и «я всего лишь убрал старый пример» — такое же
    изменение, как новый файл.
    """
    added = tuple(sorted(set(current) - set(base)))
    removed = tuple(sorted(set(base) - set(current)))
    modified = tuple(
        sorted(
            path
            for path in set(base) & set(current)
            if base[path] != current[path]
        )
    )
    return BundleChanges(added=added, modified=modified, removed=removed)


__all__ = [
    "IGNORE_FILES",
    "MAX_BUNDLE_BYTES",
    "MAX_BUNDLE_DEPTH",
    "MAX_BUNDLE_FILES",
    "MAX_UNPACKED_BYTES",
    "BundleChanges",
    "ProposalBundleError",
    "check_limits",
    "collect_files",
    "diff_files",
    "make_bundle",
    "tree_digest",
    "unpack_snapshot",
]
