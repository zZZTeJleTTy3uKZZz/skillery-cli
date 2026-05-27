"""Сканирует папку skill'а, строит manifest для publish.

- Считает sha256 каждого файла
- Извлекает frontmatter из SKILL.md (name, description, tags, triggers)
- Игнорирует препрезенвед-пути и большие бинарники
- Опционально читает _skill_meta.toml для super-skill зависимостей
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Что не включаем в manifest (тяжёлое локальное state)
_IGNORE_PATHS = (
    "_local",
    "browser_profiles",
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    "build",
    ".coverage",
    "*.egg-info",
)

# Frontmatter regex: YAML между `---` ... `---`
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL | re.MULTILINE
)


@dataclass(slots=True)
class BuiltManifest:
    version: str
    description: str
    triggers: list[str]
    tags: list[str]
    files: list[dict]  # [{path, sha256, size}, ...]
    dependencies: list[dict]
    preserved_paths: list[str]


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _should_skip(rel_path: str) -> bool:
    parts = rel_path.split("/")
    for part in parts:
        for pat in _IGNORE_PATHS:
            if pat.endswith("*"):
                if part.startswith(pat[:-1]):
                    return True
            elif part == pat:
                return True
    return False


def _read_frontmatter(skill_md: Path) -> dict[str, Any]:
    if not skill_md.exists():
        return {}
    text = skill_md.read_text(encoding="utf-8", errors="ignore")
    m = _FRONTMATTER_RE.search(text)
    if not m:
        return {}
    # Используем простой YAML-парсер (PyYAML не в зависимостях клиента).
    # Парсим только ключи name/description/triggers (как key: value либо мульти-строка `key: |\n  ...`)
    result: dict[str, Any] = {}
    yaml_body = m.group(1)
    current_key: str | None = None
    multiline_buf: list[str] = []
    for line in yaml_body.split("\n"):
        if current_key is not None:
            if line.startswith("  ") or not line.strip():
                multiline_buf.append(line[2:] if line.startswith("  ") else "")
                continue
            result[current_key] = "\n".join(multiline_buf).strip()
            current_key = None
            multiline_buf = []
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "|":
            current_key = key
            multiline_buf = []
        else:
            result[key] = value.strip("\"'")
    if current_key is not None:
        result[current_key] = "\n".join(multiline_buf).strip()
    return result


def _read_meta_toml(skill_dir: Path) -> dict[str, Any]:
    p = skill_dir / "_skill_meta.toml"
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8"))


def git_commit_sha(skill_dir: Path) -> str | None:
    if not (skill_dir / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(skill_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()[:40]
    except Exception:
        return None


def build_manifest(skill_dir: Path, *, version: str) -> BuiltManifest:
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill папка не найдена: {skill_dir}")

    fm = _read_frontmatter(skill_dir / "SKILL.md")
    meta_toml = _read_meta_toml(skill_dir)

    triggers_list: list[str] = []
    raw_triggers = meta_toml.get("triggers") or fm.get("triggers")
    if isinstance(raw_triggers, str):
        triggers_list = [t.strip() for t in raw_triggers.replace(",", " ").split() if t.strip()]
    elif isinstance(raw_triggers, list):
        triggers_list = [str(t) for t in raw_triggers]

    tags_list: list[str] = []
    raw_tags = meta_toml.get("tags") or fm.get("tags")
    if isinstance(raw_tags, str):
        tags_list = [t.strip() for t in raw_tags.replace(",", " ").split() if t.strip()]
    elif isinstance(raw_tags, list):
        tags_list = [str(t) for t in raw_tags]

    description = (
        meta_toml.get("description")
        or fm.get("description", "")
    )

    deps: list[dict] = []
    for d in meta_toml.get("dependencies", []):
        if isinstance(d, dict) and "slug" in d:
            deps.append({"slug": d["slug"], "min_version": d.get("min_version", "0.0.0")})

    # Walk file tree
    files: list[dict] = []
    for p in skill_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(skill_dir).as_posix()
        if _should_skip(rel):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        # Лимит — 5 МБ на файл (большие бинари пропускаем; обычно их и не должно быть)
        if size > 5 * 1024 * 1024:
            continue
        try:
            sha = _sha256_of(p)
        except OSError:
            continue
        files.append({"path": rel, "sha256": sha, "size": size})

    return BuiltManifest(
        version=version,
        description=description or f"{skill_dir.name} skill",
        triggers=triggers_list,
        tags=tags_list,
        files=sorted(files, key=lambda f: f["path"]),
        dependencies=deps,
        preserved_paths=["_local/", "browser_profiles/"],
    )
