"""Фильтрация файлов skill при install: .skillignore + manifest files allowlist.

Логика после git clone:
1. Если SKILL.md frontmatter содержит `files:` — это allowlist.
   Оставляем ТОЛЬКО matched paths. `.skillignore` игнорируется.
2. Иначе если `.skillignore` есть — gitignore-стиль patterns. Удаляем matched.
3. Иначе ничего не делаем.

Всегда сохраняются: `.git/`, `SKILL.md`, `.skillignore`, `_skill_meta.json`.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

import pathspec

SKILLIGNORE_FILENAME = ".skillignore"

# Корневые пути которые НИКОГДА не удаляются (даже если matched ignore-паттерн
# или НЕ matched allowlist). .git/ — для воспроизводимости; SKILL.md — основа
# skill; .skillignore сам себя; _skill_meta.json — установщик пишет позже.
_PRESERVED_ROOT_NAMES: frozenset[str] = frozenset(
    {".git", "SKILL.md", ".skillignore", "_skill_meta.json"}
)


def _on_rm_error(func, path, exc_info):
    """Чинит read-only-файлы (Windows: .git/objects/...) перед удалением."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def parse_skill_md_files_allowlist(skill_md_path: Path) -> list[str] | None:
    """Парсит SKILL.md frontmatter; возвращает list patterns если `files:` поле есть.

    Возвращает None если поле отсутствует, SKILL.md нет frontmatter, или файл не существует.
    """
    if not skill_md_path.exists():
        return None

    content = skill_md_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None

    # Найти end of frontmatter (закрывающий `---` после первого).
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return None

    frontmatter = content[3 : 3 + end_match.start()]
    files = _extract_yaml_list(frontmatter, "files")
    return files if files else None


def _extract_yaml_list(frontmatter: str, key: str) -> list[str] | None:
    """Простой парсер YAML списка из строки. Без pyyaml dep.

    Поддерживает:
    files:
      - "SKILL.md"
      - src/**
      - 'tests/*.py'

    Возвращает list или None если key не найден / список пуст.
    """
    pattern = re.compile(
        rf"^{re.escape(key)}:\s*\n((?:[ \t]+-[ \t]+.+\n?)+)",
        re.MULTILINE,
    )
    match = pattern.search(frontmatter)
    if not match:
        return None

    items_block = match.group(1)
    items: list[str] = []
    for raw_line in items_block.split("\n"):
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        value = line[2:].strip()
        # Strip surrounding quotes (single or double).
        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        if value:
            items.append(value)
    return items or None


def apply_skill_filter(
    slug_dir: Path, *, extra_preserved: tuple[str, ...] = ()
) -> dict[str, int]:
    """Применяет .skillignore И/ИЛИ manifest.files allowlist в slug_dir.

    Логика:
    - Если SKILL.md имеет `files:` → ALLOWLIST mode (оставляет только matched).
      .skillignore игнорируется в этом случае (allowlist эксплицитнее).
    - Иначе если .skillignore есть → IGNORE mode (удаляет matched).
    - Иначе ничего не делает.

    `extra_preserved` — дополнительные корневые пути (напр. `_local/`,
    `browser_profiles/`), которые НИКОГДА не удаляются. Нужно при update,
    чтобы allowlist новой версии не стёр пользовательский runtime-state.

    Возвращает {"removed": N, "kept": M}.
    `kept` НЕ учитывает файлы внутри .git/.
    """
    skill_md = slug_dir / "SKILL.md"
    allowlist_patterns = parse_skill_md_files_allowlist(skill_md)

    if allowlist_patterns is not None:
        return _apply_allowlist(slug_dir, allowlist_patterns, extra_preserved)

    skillignore = slug_dir / SKILLIGNORE_FILENAME
    if skillignore.exists():
        patterns = [
            line
            for line in skillignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if patterns:
            return _apply_ignore(slug_dir, patterns, extra_preserved)

    return {"removed": 0, "kept": _count_files(slug_dir)}


def _normalize_preserved_roots(extra_preserved: tuple[str, ...]) -> frozenset[str]:
    """Корневые имена из extra_preserved (`_local/` → `_local`, `.env` → `.env`)."""
    roots: set[str] = set()
    for p in extra_preserved:
        norm = p.replace("\\", "/").strip().strip("/")
        if not norm:
            continue
        roots.add(norm.split("/")[0])
    return frozenset(roots)


def _is_preserved(
    slug_dir: Path, path: Path, rel: Path, extra_roots: frozenset[str] = frozenset()
) -> bool:
    """True если path внутри slug_dir НЕЛЬЗЯ удалять (preserved root или внутри .git)."""
    # Файл/папка прямо в корне с защищённым именем.
    if path.parent == slug_dir and path.name in _PRESERVED_ROOT_NAMES:
        return True
    if not rel.parts:
        return False
    # Что угодно внутри .git/ или внутри extra-preserved root (_local/, ...).
    return rel.parts[0] == ".git" or rel.parts[0] in extra_roots


def _remove(path: Path) -> bool:
    """Удаляет file или dir; True если что-то удалили."""
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
    if path.is_dir():
        shutil.rmtree(path, onerror=_on_rm_error)
        return True
    return False


def _apply_ignore(
    slug_dir: Path, patterns: list[str], extra_preserved: tuple[str, ...] = ()
) -> dict[str, int]:
    spec = pathspec.PathSpec.from_lines("gitignore", patterns)
    extra_roots = _normalize_preserved_roots(extra_preserved)
    removed = 0

    # Сортировка по убыванию глубины — сначала листья, потом директории.
    # Это позволяет удалять файлы внутри директории до самой директории.
    for path in sorted(slug_dir.rglob("*"), key=lambda p: -len(p.parts)):
        if not path.exists():
            continue  # уже удалён вместе с родителем
        try:
            rel = path.relative_to(slug_dir)
        except ValueError:
            continue
        if _is_preserved(slug_dir, path, rel, extra_roots):
            continue

        rel_str = str(rel).replace("\\", "/")
        # Для директорий pathspec ожидает trailing slash, чтобы matched `tests/`.
        match_target = rel_str + "/" if path.is_dir() else rel_str
        if spec.match_file(match_target) or spec.match_file(rel_str):
            if _remove(path):
                removed += 1

    return {"removed": removed, "kept": _count_files(slug_dir)}


def _apply_allowlist(
    slug_dir: Path, patterns: list[str], extra_preserved: tuple[str, ...] = ()
) -> dict[str, int]:
    spec = pathspec.PathSpec.from_lines("gitignore", patterns)
    extra_roots = _normalize_preserved_roots(extra_preserved)
    removed = 0

    # Pass 1: удалить файлы НЕ в allowlist.
    for path in sorted(slug_dir.rglob("*"), key=lambda p: -len(p.parts)):
        if not path.exists():
            continue
        try:
            rel = path.relative_to(slug_dir)
        except ValueError:
            continue
        if _is_preserved(slug_dir, path, rel, extra_roots):
            continue
        if not path.is_file() and not path.is_symlink():
            continue

        rel_str = str(rel).replace("\\", "/")
        if not spec.match_file(rel_str):
            if _remove(path):
                removed += 1

    # Pass 2: удалить пустые директории (которые остались после удаления детей
    # и сами не в allowlist).
    for path in sorted(slug_dir.rglob("*"), key=lambda p: -len(p.parts)):
        if not path.exists() or not path.is_dir():
            continue
        try:
            rel = path.relative_to(slug_dir)
        except ValueError:
            continue
        if _is_preserved(slug_dir, path, rel, extra_roots):
            continue
        if not any(path.iterdir()):
            try:
                path.rmdir()
                removed += 1
            except OSError:
                pass

    return {"removed": removed, "kept": _count_files(slug_dir)}


def _count_files(slug_dir: Path) -> int:
    count = 0
    for p in slug_dir.rglob("*"):
        try:
            rel = p.relative_to(slug_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == ".git":
            continue
        if p.is_file():
            count += 1
    return count
