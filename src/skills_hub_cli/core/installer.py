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

# Маркер сгенерированной заглушки (см. stub-ветку _materialize_store).
# По нему отличаем НАШ stub от реального контента: stub можно заменять,
# реальный контент — НИКОГДА (P0: stub-would-clobber guard).
_STUB_SENTINEL = "Stub — установлено без git репо."

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
    - Ссылки НЕ следуются вслепую; если цель ссылки резолвится наружу dst →
      PathTraversalError. Ссылка внутрь dst копируется как обычный файл/папка
      (через материализацию содержимого).
    - Под «ссылкой» понимаем и POSIX symlink, и Windows junction (reparse
      mount-point): `Path.is_symlink()` для junction = False, а
      `os.walk(followlinks=False)` его НЕ отсекает и спустился бы внутрь →
      детект идёт через `linker.is_link` (reparse-tag).
    - Любой относительный путь с `..`, который вырвался бы за dst → отвергается.

    dst создаётся при необходимости. Существующие файлы перезаписываются.
    """
    from skills_hub_cli.core import linker

    src = Path(src)
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    dst_abs = Path(os.path.abspath(dst))
    # Реальный корень источника: ссылка безопасна только если её цель
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

        linked_dirs: list[str] = []
        for name in list(dirnames):
            child = root_path / name
            target = _assert_within(dst_abs, dst / rel_root / name)
            if linker.is_link(child):
                # Ссылка-директория (symlink ИЛИ junction): цель обязана быть
                # внутри src, иначе reject.
                real = Path(os.path.realpath(child))
                _assert_within(src_root, real)  # бросит, если наружу clone
                # Внутрь — материализуем рекурсивно как обычную папку и убираем
                # из dirnames: os.walk по symlink-папке не идёт сам, но junction
                # он НЕ отсекает (followlinks=False ловит только symlink) и
                # спустился бы внутрь, дублируя/обходя guard.
                target.mkdir(parents=True, exist_ok=True)
                safe_copy_tree(child, target)
                linked_dirs.append(name)
            else:
                target.mkdir(parents=True, exist_ok=True)
        if linked_dirs:
            dirnames[:] = [d for d in dirnames if d not in linked_dirs]

        for name in filenames:
            child = root_path / name
            target = _assert_within(dst_abs, dst / rel_root / name)
            if linker.is_link(child):
                real = Path(os.path.realpath(child))
                _assert_within(src_root, real)  # бросит, если ссылка наружу clone
            target.parent.mkdir(parents=True, exist_ok=True)
            # copy2 следует по ссылке и копирует РЕАЛЬНОЕ содержимое (цель
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
    skipped: bool = False          # установка НЕ выполнена (см. skip_reason)
    skip_reason: str | None = None  # напр. "stub-would-clobber" (P0-guard)
    content: str = "real"          # "real" | "stub" — что лежит в установке


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

    Гарантии: dst внутри slug_dir; РЕАЛЬНАЯ цель src (после резолва symlink И
    Windows junction в любом компоненте пути) лежит внутри src_root (clone), а
    не на хост-ФС (защита от malicious repo). `realpath` резолвит и junction,
    который `Path.is_symlink()` НЕ ловит, поэтому проверяем всегда — даже когда
    сам src выглядит обычным файлом, его родитель может быть ссылкой наружу.
    """
    _assert_within(slug_dir, dst)
    real = Path(os.path.realpath(src))
    try:
        _assert_within(Path(os.path.realpath(src_root)), real)
    except PathTraversalError:
        raise PathTraversalError(f"Ссылка наружу репо: {src}") from None
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


def _stub_would_clobber(path: Path) -> bool:
    """True если в path есть ЖИВОЙ контент, который нельзя затирать stub'ом.

    P0-guard (живой инцидент: auto-update скилла с repo_url=null уничтожил
    реальный ~/.claude/skills/bitrix24 112-байтовым stub'ом).

    «Живой контент» = любые файлы кроме ``_skill_meta.json`` и нашего же
    сгенерированного stub-SKILL.md (детект по ``_STUB_SENTINEL``). Пустой или
    отсутствующий каталог, голая meta и прежний stub — заменяемы (свежая
    установка / stub-over-stub разрешены).
    """
    try:
        if not path.exists():
            return False
        entries = list(path.iterdir())
    except OSError:
        return False  # битая ссылка и т.п. — терять нечего
    others = [p for p in entries if p.name != _SKILL_META_FILE]
    if not others:
        return False
    if len(others) == 1 and others[0].name == "SKILL.md" and others[0].is_file():
        try:
            return _STUB_SENTINEL not in others[0].read_text(encoding="utf-8")
        except OSError:
            return True  # не смогли прочитать — считаем живым, не трогаем
    return True


def _version_from_skill_md(skill_dir: Path, fallback: str) -> str:
    """Версия навыка из ``SKILL.md`` frontmatter / ``_skill_meta.toml``.

    Используется для git-url источника (фикс l2): раньше в meta хардкодился
    ``0.0.0-local``, хотя клон содержит реальную версию во frontmatter —
    симметрично тому, как ``--path`` читает версию локальной папки.
    """
    from skills_hub_cli.core.manifest_builder import (
        _read_frontmatter,
        _read_meta_toml,
    )

    try:
        ver = _read_meta_toml(skill_dir).get("version")
        if not ver:
            ver = _read_frontmatter(skill_dir / "SKILL.md").get("version")
    except Exception:  # повреждённый frontmatter не валит install
        ver = None
    ver_str = str(ver).strip() if ver else ""
    return ver_str or fallback


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
        local_src: Path | None = None,
        git_ref: str | None = None,
    ) -> InstallResult:
        """Материализует навык в стор и линкует в scope агента.

        Источник контента (взаимоисключающие):
        - ``repo_url`` задан → git clone (hub: тег ``v<version>``; git-url:
          явный ``git_ref`` — ветка/тег/sha как есть);
        - ``local_src`` задан (а ``repo_url`` нет) → копируем локальную папку
          через ``safe_copy_tree`` (тот же traversal-guard) — без сети;
        - оба None → stub-навык (как раньше).

        ``git_ref`` отличает git-url источник от hub: при нём ref берётся
        дословно (не ``v<version>``) и source в meta = "git-url".
        """
        scope = "project" if project is not None else "global"
        dir_name = skill_dir_name(slug, skill_id)
        skill_id_str = str(skill_id) if skill_id is not None else None
        store_dir = self._store_path(dir_name)
        link = self._target.slug_dir(dir_name, project=project)

        # P0-guard: stub-источник (нет ни repo_url, ни local_src) НЕ имеет
        # права заменить существующую НЕпустую установку — ни стор, ни
        # copy-scope (живой инцидент: stub затёр реальный bitrix24). Guard
        # жёсткий: --force его НЕ обходит. Stub разрешён только в пустое /
        # stub-место.
        is_stub_source = repo_url is None and local_src is None
        if is_stub_source and (
            _stub_would_clobber(store_dir) or _stub_would_clobber(link)
        ):
            return InstallResult(
                slug=slug, version=version, target_dir=link, is_update=False,
                scope=scope, skill_id=skill_id_str, store_dir=store_dir,
                linked=False, link_kind="none",
                skipped=True, skip_reason="stub-would-clobber", content="stub",
            )

        # 1. Материализация контента в центральный стор (idempotent).
        mat = self._materialize_store(
            slug=slug, dir_name=dir_name, version=version, commit_sha=commit_sha,
            repo_url=repo_url, manifest=manifest, scope=scope, project=project,
            skill_id=skill_id_str, local_src=local_src, git_ref=git_ref,
        )
        # git-url источник мог уточнить версию из frontmatter клона (фикс l2).
        effective_version = mat.get("version") or version

        # 2. Линковка стор → scope агента (fallback на copy).
        linked, link_kind = self._link_into_scope(link, store_dir, force=force)

        return InstallResult(
            slug=slug, version=effective_version, target_dir=link,
            is_update=mat["is_update"], scope=scope,
            filter_result=mat["filter_result"],
            update_diff=mat["update_diff"], skill_id=skill_id_str,
            store_dir=store_dir, linked=linked, link_kind=link_kind,
            content="stub" if is_stub_source else "real",
        )

    def _materialize_store(
        self, *, slug, dir_name, version, commit_sha, repo_url, manifest,
        scope, project, skill_id, local_src: Path | None = None,
        git_ref: str | None = None,
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
                skill_id=skill_id, local_src=local_src, git_ref=git_ref,
            )
            return {"is_update": True, "filter_result": up.filter_result,
                    "update_diff": up.update_diff, "version": up.version}

        store_dir.parent.mkdir(parents=True, exist_ok=True)
        filter_result: dict[str, int] | None = None
        source = "hub"
        if repo_url:
            with self._clone_version(repo_url, version, ref=git_ref) as cloned:
                safe_copy_tree(cloned, store_dir)
            filter_result = apply_skill_filter(store_dir)
            if git_ref is not None:
                source = "git-url"  # явный ref ⇒ произвольный git-репо, не hub
                # Версия из frontmatter клона (как у --path), не хардкод
                # '0.0.0-local' (фикс l2).
                version = _version_from_skill_md(store_dir, version)
        elif local_src is not None:
            # Локальный источник: копируем папку как навык через тот же
            # traversal-guard (safe_copy_tree junction-aware). НЕ stub.
            safe_copy_tree(Path(local_src), store_dir)
            filter_result = apply_skill_filter(store_dir)
            source = "local-path"
        else:
            store_dir.mkdir(parents=True, exist_ok=True)
            (store_dir / "SKILL.md").write_text(
                f"---\nname: {dir_name}\nversion: {version}\n---\n\n# {dir_name}\n\n"
                f"{_STUB_SENTINEL}\n",
                encoding="utf-8",
            )
        write_meta(
            store_dir,
            self._build_meta(
                slug, version, commit_sha, manifest, scope, project, skill_id,
                source=source, repo_url=repo_url,
            ),
        )
        return {"is_update": False, "filter_result": filter_result,
                "update_diff": None, "version": version}

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

    def link_existing(
        self, dir_name: str, *, project: Path | None = None, force: bool = False
    ) -> tuple[bool, str] | None:
        """Линкует уже materialized стор-навык в scope (без сети). None если в сторе нет."""
        store_dir = self._store_path(dir_name)
        if not store_dir.exists():
            return None
        link = self._target.slug_dir(dir_name, project=project)
        return self._link_into_scope(link, store_dir, force=force)

    def migrate_scope(
        self, *, project: Path | None = None, dry_run: bool = False
    ) -> dict[str, list]:
        """Переводит наши copy-установки в scope на модель стор+ссылка.

        - copy (есть _skill_meta.json, не ссылка) → перенос в стор + ссылка;
        - foreign (нет meta) → пропуск;
        - already-linked (ссылка) → пропуск.
        dry_run только собирает план, ничего не меняя.
        """
        from skills_hub_cli.core import linker

        base = self._target.base_dir(project=project)
        report: dict[str, list] = {
            "migrated": [], "skipped_foreign": [], "skipped_linked": [], "failed": [],
        }
        if not base.exists():
            return report
        for d in sorted(base.iterdir(), key=lambda p: p.name):
            try:
                if linker.is_link(d):
                    report["skipped_linked"].append(d.name)
                    continue
                if not d.is_dir():
                    continue
                if read_meta(d) is None:
                    report["skipped_foreign"].append(d.name)
                    continue
                if dry_run:
                    report["migrated"].append(d.name)
                    continue
                dir_name = d.name
                store_dir = self._store_path(dir_name)
                if store_dir.exists():
                    _force_rmtree(d)  # стор уже есть → копию убираем
                else:
                    store_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(d), str(store_dir))
                link = self._target.slug_dir(dir_name, project=project)
                self._link_into_scope(link, store_dir, force=True)
                report["migrated"].append(dir_name)
            except Exception as e:  # noqa: BLE001
                report["failed"].append({"name": d.name, "error": str(e)})
        return report

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
        local_src: Path | None = None,
        git_ref: str | None = None,
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
        local_src (локальный источник) — пере-копируем папку целиком (sha-diff
        не нужен, у локального навыка нет manifest-инвентаря). git_ref (git-url
        источник) — пере-клонируем ref и копируем дерево целиком (manifest у
        git-url пустой, sha-diff неприменим).
        """
        old_meta = read_meta(slug_dir) or {}
        old_manifest = old_meta.get("manifest") or {}

        # Источники без manifest-инвентаря (local-path / git-url) обновляем
        # пере-материализацией всего дерева, сохраняя preserved-пути.
        if local_src is not None or (repo_url and git_ref is not None):
            preserved = _preserved_for(manifest, self._target)
            if local_src is not None:
                safe_copy_tree(Path(local_src), slug_dir)
                src_label = "local-path"
                meta_repo = None
            else:
                with self._clone_version(repo_url, version, ref=git_ref) as cloned:
                    safe_copy_tree(cloned, slug_dir)
                src_label = "git-url"
                meta_repo = repo_url
                # Версия из frontmatter свежего клона (фикс l2).
                version = _version_from_skill_md(slug_dir, version)
            filter_result = apply_skill_filter(slug_dir, extra_preserved=preserved)
            write_meta(
                slug_dir,
                self._build_meta(
                    slug, version, commit_sha, manifest, scope, project, skill_id,
                    source=src_label, repo_url=meta_repo,
                ),
            )
            return InstallResult(
                slug=slug, version=version, target_dir=slug_dir,
                is_update=True, scope=scope, skill_id=skill_id,
                filter_result=filter_result,
            )

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
                slug, version, commit_sha, manifest, scope, project, skill_id,
                source="hub", repo_url=repo_url,
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
    def _clone_version(self, repo_url: str, version: str, *, ref: str | None = None):
        """git clone во временную папку (yield Path).

        Поведение по ``ref``:
        - ``None`` → hub-релиз, клонируется тег ``v<version>``;
        - ``""`` (пусто) → git-url без явного ref: клон дефолтной ветки репо
          (``--branch`` опускается — ``--branch HEAD`` git не принимает);
        - иначе → дословный ref (ветка/тег/sha).
        Папка удаляется при выходе из контекста.
        """
        use_default_branch = ref == ""
        ref = ref if ref is not None else f"v{version}"
        url = _authenticated_url(repo_url)
        tmp = Path(tempfile.mkdtemp(prefix="skills-hub-clone-"))
        clone_dir = tmp / "repo"
        cmd = ["git", "clone", "--depth", "1"]
        if not use_default_branch:
            cmd += ["--branch", ref]
        cmd += [url, str(clone_dir)]
        try:
            try:
                subprocess.run(
                    cmd,
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
        source: str = "hub",
        repo_url: str | None = None,
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
            "source": source,  # "hub" | "local-path" | "git-url"
            "repo_url": repo_url,  # origin для git-url; None у hub/local
        }
