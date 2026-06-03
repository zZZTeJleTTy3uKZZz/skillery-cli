"""Установщик skills: git clone (если есть repo) + write meta.

Поддерживает global и per-project scope (через IAgentTarget.slug_dir(project=...)).
Перепринимает все основные методы с project: Path | None.

Флоу:
- install (fresh): git clone версии → temp → safe_copy_tree в slug_dir →
  apply_skill_filter → write meta.
- update (incremental, ТЗ §8.2): читаем старый manifest → git clone новой
  версии → temp → diff по sha256 → копируем added+changed → удаляем orphan
  (кроме preserved_paths) → apply_skill_filter → write meta.
- remove (ТЗ §8): удаляем slug_dir; --keep-local сохраняет preserved_paths.

Все копирования из cloned-репо проходят через `safe_copy_tree`, который
отвергает path-traversal (`..`, абсолютные пути, symlink наружу) — ТЗ §10.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skills_hub_cli.core.agents.base import IAgentTarget
from skills_hub_cli.core.skill_filter import apply_skill_filter


class PathTraversalError(RuntimeError):
    """Cloned-репо пытается записать файл наружу slug_dir (symlink / `..` / abs)."""


def _on_rm_error(func, path, exc_info):  # noqa: ANN001
    """Чинит read-only-файлы в Windows (.git/objects/...) перед удалением."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _force_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_on_rm_error)


_SKILL_META_FILE = "_skill_meta.json"

# Внутри source-репо НЕ копируем в slug_dir (state репозитория, не skill).
_COPY_SKIP_ROOT = frozenset({".git"})


def skill_dir_name(slug: str | None, skill_id: str | int | None = None) -> str:
    """Имя on-disk папки skill'а: slug если задан, иначе числовой id (PK-миграция).

    slug стал опциональным (см. ``PK_MIGRATION_DESIGN.md`` §3.E): для slug-less
    скилла идентичностью каталога (и ключом ``_skill_meta.json``) становится
    его числовой ``id``. Пустая строка трактуется как «slug не задан».

    Бросает ``ValueError`` если нет ни slug, ни id — каталог некуда положить.
    """
    if slug:
        return slug
    if skill_id is not None and str(skill_id) != "":
        return str(skill_id)
    raise ValueError("Невозможно определить папку skill'а: нет ни slug, ни id")


def _assert_within(base: Path, candidate: Path) -> Path:
    """Проверяет что candidate лежит ВНУТРИ base (после нормализации `..`).

    Не требует существования candidate (используем os.path.normpath, не resolve).
    Возвращает нормализованный путь или бросает PathTraversalError.
    """
    base_abs = os.path.abspath(base)
    cand_abs = os.path.abspath(candidate)
    # commonpath бросает ValueError на разных дисках (Windows) → traversal.
    try:
        common = os.path.commonpath([base_abs, cand_abs])
    except ValueError as e:
        raise PathTraversalError(f"Путь вне slug_dir: {candidate}") from e
    if common != base_abs:
        raise PathTraversalError(f"Путь вне slug_dir: {candidate}")
    return Path(cand_abs)


def safe_copy_tree(src: Path, dst: Path) -> None:
    """Копирует содержимое src → dst, отвергая path-traversal (ТЗ §10).

    Правила безопасности:
    - `.git/` source-репо пропускается (это state репо, не содержимое skill).
    - Симлинки НЕ следуются; если symlink-цель резолвится наружу dst →
      PathTraversalError. Симлинк внутрь dst копируется как обычный файл/папка
      (через материализацию содержимого).
    - Любой относительный путь с `..`, который вырвался бы за dst → отвергается.

    dst создаётся при необходимости. Существующие файлы перезаписываются.
    """
    src = Path(src)
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    dst_abs = Path(os.path.abspath(dst))
    # Реальный корень источника: symlink безопасен только если его цель
    # резолвится ВНУТРЬ src (clone). Escape наружу = host-FS / секрет → reject.
    src_root = Path(os.path.realpath(src))

    for root, dirnames, filenames in os.walk(src, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(src)
        # Пропускаем .git/ и не спускаемся внутрь.
        if rel_root.parts and rel_root.parts[0] in _COPY_SKIP_ROOT:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _COPY_SKIP_ROOT]

        for name in list(dirnames):
            child = root_path / name
            target = _assert_within(dst_abs, dst / rel_root / name)
            if child.is_symlink():
                # Симлинк-директория: цель обязана быть внутри src, иначе reject.
                real = Path(os.path.realpath(child))
                _assert_within(src_root, real)  # бросит, если наружу clone
                # Внутрь — материализуем рекурсивно как обычную папку,
                # os.walk сам по symlink-папке не пойдёт (followlinks=False).
                target.mkdir(parents=True, exist_ok=True)
                safe_copy_tree(child, target)
            else:
                target.mkdir(parents=True, exist_ok=True)

        for name in filenames:
            child = root_path / name
            target = _assert_within(dst_abs, dst / rel_root / name)
            if child.is_symlink():
                real = Path(os.path.realpath(child))
                _assert_within(src_root, real)  # бросит, если symlink наружу clone
            target.parent.mkdir(parents=True, exist_ok=True)
            # copy2 следует по симлинку и копирует РЕАЛЬНОЕ содержимое (цель
            # уже проверена что внутри src).
            shutil.copy2(child, target)


def _authenticated_url(url: str) -> str:
    if "@" in url or not url.startswith("https://"):
        return url
    token = os.environ.get("SKILLS_HUB_GIT_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if not token:
        return url
    return url.replace("https://", f"https://oauth2:{token}@", 1)


@dataclass
class InstallResult:
    slug: str | None  # None для slug-less skill (PK-миграция); тогда identity = id
    version: str
    target_dir: Path
    is_update: bool
    scope: str  # "global" | "project"
    filter_result: dict[str, int] | None = None  # {"removed": N, "kept": M} после filter
    update_diff: dict[str, int] | None = None  # {"added": A, "changed": C, "removed": R} при update
    skill_id: str | None = None  # числовой id (строкой); identity папки для slug-less
    store_dir: Path | None = None  # путь в центральном сторе (источник контента)
    linked: bool = False           # True если scope-ссылка; False если copy-fallback
    link_kind: str = "copy"        # "junction" | "symlink" | "copy"


@dataclass
class RemoveResult:
    slug: str | None
    target_dir: Path
    scope: str
    removed: bool  # удалили ли что-то
    kept_local: bool  # сохранили ли preserved_paths (--keep-local)
    skill_id: str | None = None
    purged: bool = False  # удалён ли навык из стора (--purge)


def write_meta(slug_dir: Path, meta: dict[str, Any]) -> None:
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / _SKILL_META_FILE).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def read_meta(slug_dir: Path) -> dict[str, Any] | None:
    p = slug_dir / _SKILL_META_FILE
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# Дефолтные preserved-пути, если их нет ни в manifest, ни у target.
_DEFAULT_PRESERVED: tuple[str, ...] = ("_local/", "browser_profiles/")


def _file_index(manifest: dict[str, Any]) -> dict[str, str]:
    """{rel_path: sha256} из manifest['files']."""
    idx: dict[str, str] = {}
    for f in manifest.get("files") or []:
        path = f.get("path")
        sha = f.get("sha256")
        if path:
            idx[path] = sha or ""
    return idx


def _manifest_diff(
    old: dict[str, Any], new: dict[str, Any]
) -> tuple[list[str], list[str], list[str]]:
    """Возвращает (added, changed, removed) rel-пути для перехода old → new.

    Зеркалит domain SkillManifest.diff: removed исключает preserved_paths новой
    версии (фактическое удаление с диска делается с учётом target.preserved тоже).
    """
    old_idx = _file_index(old)
    new_idx = _file_index(new)
    preserved = tuple(new.get("preserved_paths") or ())

    added = [p for p in new_idx if p not in old_idx]
    changed = [p for p in new_idx if p in old_idx and old_idx[p] != new_idx[p]]
    removed = [
        p
        for p in old_idx
        if p not in new_idx and not any(p.startswith(pp) for pp in preserved)
    ]
    return sorted(added), sorted(changed), sorted(removed)


def _preserved_for(manifest: dict[str, Any], target: IAgentTarget) -> tuple[str, ...]:
    """Объединяет preserved_paths из manifest + target + дефолты."""
    out: list[str] = list(_DEFAULT_PRESERVED)
    out.extend(manifest.get("preserved_paths") or [])
    with contextlib.suppress(Exception):
        out.extend(target.preserved_paths())
    # Нормализуем и дедуплицируем.
    seen: set[str] = set()
    result: list[str] = []
    for p in out:
        norm = p.strip()
        if norm and norm not in seen:
            seen.add(norm)
            result.append(norm)
    return tuple(result)


def _is_preserved_rel(rel: str, preserved: tuple[str, ...]) -> bool:
    """True если rel-путь попадает под один из preserved-префиксов.

    preserved элементы могут быть с trailing slash (`_local/`) или без (`.env`).
    """
    rel_norm = rel.replace("\\", "/")
    for p in preserved:
        p_norm = p.replace("\\", "/")
        if p_norm.endswith("/"):
            prefix = p_norm
            if rel_norm == prefix.rstrip("/") or rel_norm.startswith(prefix):
                return True
        else:
            if rel_norm == p_norm or rel_norm.startswith(p_norm + "/"):
                return True
    return False


def _safe_copy_file(src: Path, dst: Path, slug_dir: Path, src_root: Path) -> None:
    """Копирует один файл src → dst.

    Гарантии: dst внутри slug_dir; если src — symlink, его цель резолвится
    внутрь src_root (clone), а не на хост-ФС (защита от malicious repo).
    """
    _assert_within(slug_dir, dst)
    if src.is_symlink():
        real = Path(os.path.realpath(src))
        try:
            _assert_within(Path(os.path.realpath(src_root)), real)
        except PathTraversalError:
            raise PathTraversalError(f"Symlink наружу репо: {src}") from None
    shutil.copy2(src, dst)


def _prune_empty_parents(start: Path, stop: Path) -> None:
    """Удаляет пустые директории вверх от start до (не включая) stop."""
    cur = start
    stop_abs = os.path.abspath(stop)
    while os.path.abspath(cur) != stop_abs:
        try:
            if cur.is_dir() and not any(cur.iterdir()):
                cur.rmdir()
            else:
                break
        except OSError:
            break
        cur = cur.parent


class SkillInstaller:
    """MVP: install через git clone + write meta. Scope = global | project."""

    def __init__(self, target: IAgentTarget, store_dir: Path | None = None) -> None:
        self._target = target
        # Лениво, чтобы не тянуть config на уровне модуля.
        from skills_hub_cli.config import _default_store_dir

        self._store_dir = Path(store_dir) if store_dir is not None else _default_store_dir()

    def _store_path(self, dir_name: str) -> Path:
        return self._store_dir / dir_name

    def install(
        self,
        *,
        slug: str | None,
        version: str,
        commit_sha: str,
        repo_url: str | None,
        manifest: dict[str, Any],
        project: Path | None = None,
        force: bool = False,
        skill_id: str | int | None = None,
    ) -> InstallResult:
        scope = "project" if project is not None else "global"
        dir_name = skill_dir_name(slug, skill_id)
        skill_id_str = str(skill_id) if skill_id is not None else None
        store_dir = self._store_path(dir_name)

        # 1. Материализация контента в центральный стор (idempotent).
        mat = self._materialize_store(
            slug=slug, dir_name=dir_name, version=version, commit_sha=commit_sha,
            repo_url=repo_url, manifest=manifest, scope=scope, project=project,
            skill_id=skill_id_str,
        )

        # 2. Линковка стор → scope агента (fallback на copy).
        link = self._target.slug_dir(dir_name, project=project)
        linked, link_kind = self._link_into_scope(link, store_dir, force=force)

        return InstallResult(
            slug=slug, version=version, target_dir=link, is_update=mat["is_update"],
            scope=scope, filter_result=mat["filter_result"],
            update_diff=mat["update_diff"], skill_id=skill_id_str,
            store_dir=store_dir, linked=linked, link_kind=link_kind,
        )

    def _materialize_store(
        self, *, slug, dir_name, version, commit_sha, repo_url, manifest,
        scope, project, skill_id,
    ) -> dict[str, Any]:
        """Кладёт контент навыка в store_dir. Возвращает {is_update, filter_result, update_diff}."""
        store_dir = self._store_path(dir_name)
        has_our_meta = store_dir.exists() and read_meta(store_dir) is not None
        is_foreign = store_dir.exists() and not has_our_meta
        if is_foreign:
            _force_rmtree(store_dir)  # стор — наш каталог, аномалию перезатираем
            has_our_meta = False

        if has_our_meta:
            up = self._do_update(
                slug=slug, version=version, commit_sha=commit_sha, repo_url=repo_url,
                manifest=manifest, scope=scope, project=project, slug_dir=store_dir,
                skill_id=skill_id,
            )
            return {"is_update": True, "filter_result": up.filter_result,
                    "update_diff": up.update_diff}

        store_dir.parent.mkdir(parents=True, exist_ok=True)
        filter_result: dict[str, int] | None = None
        if repo_url:
            with self._clone_version(repo_url, version) as cloned:
                safe_copy_tree(cloned, store_dir)
            filter_result = apply_skill_filter(store_dir)
        else:
            store_dir.mkdir(parents=True, exist_ok=True)
            (store_dir / "SKILL.md").write_text(
                f"---\nname: {dir_name}\nversion: {version}\n---\n\n# {dir_name}\n\n"
                "Stub — установлено без git репо.\n",
                encoding="utf-8",
            )
        write_meta(
            store_dir,
            self._build_meta(slug, version, commit_sha, manifest, scope, project, skill_id),
        )
        return {"is_update": False, "filter_result": filter_result, "update_diff": None}

    def _link_into_scope(
        self, link: Path, store_dir: Path, *, force: bool
    ) -> tuple[bool, str]:
        """Создаёт ссылку scope→стор. Возвращает (linked, link_kind)."""
        from skills_hub_cli.core import linker

        if not linker.is_link(link) and link.exists():
            has_meta = read_meta(link) is not None
            if not has_meta and not force:
                raise RuntimeError(
                    f"Папка {link} уже существует и не управляется skills-hub "
                    "(нет _skill_meta.json). Перезаписать через --force "
                    "(удалит содержимое!) или удалить вручную."
                )
            _force_rmtree(link)  # наша copy-fallback ИЛИ --force → заменяем ссылкой
        try:
            kind = linker.create_link(link, store_dir)
            return True, kind
        except OSError:
            # Нет прав / ФС не поддерживает ссылки → копируем стор в scope.
            safe_copy_tree(store_dir, link)
            return False, "copy"

    # ------------------------------------------------------------------
    #  incremental update (ТЗ §8.2)
    # ------------------------------------------------------------------
    def _do_update(
        self,
        *,
        slug: str | None,
        version: str,
        commit_sha: str,
        repo_url: str | None,
        manifest: dict[str, Any],
        scope: str,
        project: Path | None,
        slug_dir: Path,
        skill_id: str | None = None,
    ) -> InstallResult:
        """Докачивает diff между установленной и новой версией.

        Алгоритм:
        1. читаем старый manifest из _skill_meta.json,
        2. git clone новой версии → temp (если есть repo_url),
        3. diff(old, new) по sha256 → (added, changed, removed),
        4. копируем added+changed из temp в slug_dir (через traversal-guard),
        5. удаляем removed, КРОМЕ preserved_paths,
        6. apply_skill_filter,
        7. write meta.

        Без repo_url (stub-режим) — diff пропускаем, только обновляем meta.
        """
        old_meta = read_meta(slug_dir) or {}
        old_manifest = old_meta.get("manifest") or {}

        if not repo_url:
            # Stub-режим: нечего докачивать, только meta.
            write_meta(
                slug_dir,
                self._build_meta(
                    slug, version, commit_sha, manifest, scope, project, skill_id
                ),
            )
            return InstallResult(
                slug=slug, version=version, target_dir=slug_dir,
                is_update=True, scope=scope, skill_id=skill_id,
            )

        added, changed, removed = _manifest_diff(old_manifest, manifest)
        preserved = _preserved_for(manifest, self._target)

        filter_result: dict[str, int] | None = None
        with self._clone_version(repo_url, version) as cloned:
            # 4. Копируем added + changed.
            for rel in [*added, *changed]:
                src_file = cloned / rel
                if not src_file.exists():
                    continue  # manifest упоминает файл, но в репо его нет — пропуск
                dst_file = _assert_within(slug_dir, slug_dir / rel)
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                _safe_copy_file(src_file, dst_file, slug_dir, cloned)
        # 5. Удаляем orphan (нет в new manifest) кроме preserved.
        for rel in removed:
            if _is_preserved_rel(rel, preserved):
                continue
            victim = slug_dir / rel
            if victim.is_symlink() or victim.is_file():
                victim.unlink(missing_ok=True)
            elif victim.is_dir():
                _force_rmtree(victim)
            # Подчищаем опустевшие родительские директории.
            _prune_empty_parents(victim.parent, slug_dir)

        # 6. Фильтр (.skillignore / files allowlist новой версии). Передаём
        #    preserved-пути, чтобы allowlist НЕ стёр пользовательский _local/.
        filter_result = apply_skill_filter(slug_dir, extra_preserved=preserved)

        # 7. meta.
        write_meta(
            slug_dir,
            self._build_meta(
                slug, version, commit_sha, manifest, scope, project, skill_id
            ),
        )
        return InstallResult(
            slug=slug,
            version=version,
            target_dir=slug_dir,
            is_update=True,
            scope=scope,
            filter_result=filter_result,
            skill_id=skill_id,
            update_diff={
                "added": len(added),
                "changed": len(changed),
                "removed": len(removed),
            },
        )

    # ------------------------------------------------------------------
    #  remove / uninstall (ТЗ §8)
    # ------------------------------------------------------------------
    def remove(
        self,
        *,
        slug: str | None,
        project: Path | None = None,
        keep_local: bool = False,
        skill_id: str | int | None = None,
        purge: bool = False,
    ) -> RemoveResult:
        """Снимает скилл из scope. Ссылка → remove_link (стор цел); copy → прежняя
        логика keep_local. `purge` дополнительно удаляет навык из стора."""
        from skills_hub_cli.core import linker

        scope = "project" if project is not None else "global"
        dir_name = skill_dir_name(slug, skill_id)
        skill_id_str = str(skill_id) if skill_id is not None else None
        link = self._target.slug_dir(dir_name, project=project)

        removed = False
        kept_local = False
        if linker.is_link(link):
            linker.remove_link(link)
            removed = True
        elif link.exists():
            removed, kept_local = self._remove_copy(link, keep_local)

        purged = False
        if purge:
            store_dir = self._store_path(dir_name)
            if store_dir.exists():
                _force_rmtree(store_dir)
                purged = True

        return RemoveResult(
            slug=slug, target_dir=link, scope=scope, removed=(removed or purged),
            kept_local=kept_local, skill_id=skill_id_str, purged=purged,
        )

    def _remove_copy(self, slug_dir: Path, keep_local: bool) -> tuple[bool, bool]:
        """Удаляет copy-папку (не ссылку) в scope. Возвращает (removed, kept_local)."""
        if not keep_local:
            _force_rmtree(slug_dir)
            return True, False
        meta = read_meta(slug_dir) or {}
        preserved = _preserved_for(meta.get("manifest") or {}, self._target)
        for child in list(slug_dir.iterdir()):
            rel = child.name
            if _is_preserved_rel(rel, preserved) or _is_preserved_rel(rel + "/", preserved):
                continue
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                _force_rmtree(child)
        if not list(slug_dir.iterdir()):
            _force_rmtree(slug_dir)
            return True, False
        return True, True

    # ------------------------------------------------------------------
    #  helpers
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _clone_version(self, repo_url: str, version: str):
        """git clone версии v<version> во временную папку (yield Path).

        Папка удаляется при выходе из контекста.
        """
        ref = f"v{version}"
        url = _authenticated_url(repo_url)
        tmp = Path(tempfile.mkdtemp(prefix="skills-hub-clone-"))
        clone_dir = tmp / "repo"
        try:
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", ref, url, str(clone_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or e.stdout or "").replace(
                    os.environ.get("SKILLS_HUB_GIT_TOKEN", "***"), "***"
                ).replace(os.environ.get("GITLAB_TOKEN", "***"), "***")
                raise RuntimeError(f"git clone failed: {stderr}") from e
            yield clone_dir
        finally:
            _force_rmtree(tmp)

    def _build_meta(
        self,
        slug: str | None,
        version: str,
        commit_sha: str,
        manifest: dict[str, Any],
        scope: str,
        project: Path | None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "slug": slug,
            "skill_id": skill_id,  # PK-миграция §3.E: identity для slug-less skill
            "version": version,
            "commit_sha": commit_sha,
            "manifest": manifest,
            "agent": self._target.name,
            "scope": scope,
            "project": str(project) if project else None,
        }
