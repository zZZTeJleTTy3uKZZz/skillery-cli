"""Expose released payloads as hard links without sharing private runtime files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

from corekit.filelock import FileLock
from librarykit.config_util import atomic_write_text
from skillkit import linker
from skillkit.installer import read_meta
from skillkit.installer.projections import check_source

from skillery_cli.config import ClientConfig, _default_config_dir
from skillery_cli.core import local_collections

MARKER = ".skillery-view.json"
_PRIVATE = {".env", ".git", ".pkgsrc", "_local", "browser_profiles", "auth", "credentials", "sessions"}


def _digest(path: Path) -> str:
    if linker.is_link(path) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Payload view requires regular files")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read(path: Path, limit: int) -> dict:
    if linker.is_link(path) or path.stat().st_size > limit:
        raise ValueError("Invalid payload view state")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("Invalid payload view state")
    return data


def _registry() -> tuple[Path, dict]:
    path = _default_config_dir() / "payload-views.json"
    data = _read(path, 65536) if path.exists() else {"schema": 1, "views": []}
    if not isinstance(data.get("views"), list) or len(data["views"]) > 32:
        raise ValueError("Invalid payload view registry")
    for entry in data["views"]:
        if (not isinstance(entry, dict) or any(not isinstance(entry.get(key), str)
                or not entry[key] or len(entry[key]) > 4096
                for key in ("path", "store", "collection", "identity"))):
            raise ValueError("Invalid payload view registry")
    return path, data


def _write(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _relative(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid payload path")
    path = PurePosixPath(value)
    if (not value or not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value
            or ":" in value or path.as_posix() != value
            or path.parts[0].lower() in _PRIVATE):
        raise ValueError("Invalid or private payload path")
    return value


def _plan(cfg: ClientConfig, collection: str) -> dict[str, tuple[Path, str]]:
    local_collections.validate_name(collection)
    profile = local_collections.get(collection)
    if profile is None:
        raise ValueError("Local collection does not exist")
    result = {}
    for slug in profile["skills"]:
        if _relative(slug) != slug or "/" in slug:
            raise ValueError("Invalid payload skill identity")
        source = cfg.effective_store_dir() / slug
        check_source(source)
        meta = read_meta(source) or {}
        if meta.get("source") != "hub":
            raise ValueError("Only verified Hub payloads can be exposed")
        files = (meta.get("manifest") or {}).get("files") or []
        if not files:
            raise ValueError("Released file inventory is required")
        for item in files:
            relative = _relative(item["path"])
            path = source / relative
            if any(linker.is_link(parent) for parent in path.parents if parent != source and source in parent.parents):
                raise ValueError("Payload directory links are not allowed")
            if _digest(path) != item["sha256"]:
                raise ValueError("Released payload bytes have changed")
            result[slug + "/" + relative] = (path, item["sha256"])
        metadata = source / "_skill_meta.json"
        result[slug + "/_skill_meta.json"] = (metadata, _digest(metadata))
    if not result or len(result) > 8192:
        raise ValueError("Payload view file limit exceeded")
    return result


def _directories(files) -> set[str]:
    result = set()
    for relative in files:
        result.update(parent.as_posix() for parent in PurePosixPath(relative).parents
                      if parent.as_posix() != ".")
    return result


def _refresh(cfg: ClientConfig, entry: dict, plan=None) -> dict:
    root = Path(entry["path"])
    store = cfg.effective_store_dir().resolve()
    if (not root.is_absolute() or root.resolve() == store
            or root.resolve().is_relative_to(store) or store.is_relative_to(root.resolve())
            or entry["store"] != str(store) or linker.is_link(root)):
        raise ValueError("Payload view source or target binding changed")
    plan = _plan(cfg, entry["collection"]) if plan is None else plan
    old = {"schema": 1, "identity": entry["identity"], "files": {}, "directories": []}
    marker = root / MARKER
    if root.exists():
        old = _read(marker, 2097152)
        if old.get("identity") != entry["identity"] or old.get("store") != str(store):
            raise ValueError("Payload view ownership changed")
    old_files = old["files"]
    if (not isinstance(old_files, dict) or not isinstance(old.get("directories", []), list)
            or any(not isinstance(value, str) for value in old.get("directories", []))
            or any(not isinstance(value, str) or len(value) != 64
                   or any(c not in "0123456789abcdef" for c in value)
                   for value in old_files.values())):
        raise ValueError("Invalid payload view manifest")
    directories = set(old.get("directories", [])) | _directories(plan)
    if len(directories) > 16384 or len(old_files) > 8192:
        raise ValueError("Payload view state limit exceeded")
    for relative in (*old_files, *directories):
        _relative(relative)
    if root.exists():
        for current, dirs, files in os.walk(root, followlinks=False):
            parent = Path(current)
            for name in dirs:
                path = parent / name
                if linker.is_link(path) or path.relative_to(root).as_posix() not in directories:
                    raise ValueError("Unknown directory in payload view was preserved")
            for name in files:
                path = parent / name
                relative = path.relative_to(root).as_posix()
                if relative == MARKER:
                    continue
                if relative not in old_files:
                    raise ValueError("Unknown file in payload view was preserved")
                expected = plan.get(relative)
                shared_current = expected and path.samefile(expected[0]) and _digest(path) == expected[1]
                if not shared_current and _digest(path) != old_files[relative]:
                    raise ValueError("Edited payload view was preserved")
    root.mkdir(parents=True, exist_ok=True)
    state = {"schema": 1, "managed_by": "skillery", "identity": entry["identity"],
             "store": str(store), "collection": entry["collection"],
             "directories": sorted(directories),
             "files": {**old_files, **{p: value[1] for p, value in plan.items()}},
             "building": True}
    _write(marker, state)
    for relative in sorted(directories, key=lambda name: (name.count("/"), name)):
        (root / relative).mkdir(exist_ok=True)
    linked = 0
    # Metadata is published last, after all released bytes in this view.
    for relative, (source, digest) in sorted(plan.items(), key=lambda item: item[0].endswith("/_skill_meta.json")):
        target = root / relative
        if target.exists() and target.samefile(source):
            continue
        temporary = target.with_name(".skillery-next-" + uuid.uuid4().hex)
        try:
            os.link(source, temporary)
            if _digest(temporary) != digest:
                raise ValueError("Source changed while exposing payload")
            temporary.replace(target)
            linked += 1
        finally:
            if temporary.exists():
                temporary.unlink()
    for relative in old_files.keys() - plan.keys():
        target = root / relative
        if target.exists():
            if _digest(target) != old_files[relative]:
                raise ValueError("Retired payload changed and was preserved")
            target.unlink()
    if any(_digest(root / relative) != digest for relative, (_, digest) in plan.items()):
        raise ValueError("Source changed before view acceptance")
    state["building"] = False
    state["files"] = {p: value[1] for p, value in plan.items()}
    _write(marker, state)
    return {"path": str(root), "collection": entry["collection"], "files": len(plan),
            "skills": len({PurePosixPath(p).parts[0] for p in plan}), "hardlinks_created": linked,
            "payload_copied": False, "runtime_files_shared": False}


def expose_payload_view(cfg: ClientConfig, *, collection: str, path: Path) -> dict:
    """Register an explicit data-only view; fail rather than copy across volumes."""
    plan = _plan(cfg, collection)
    path = path.expanduser().absolute()
    store = cfg.effective_store_dir().resolve()
    if (path.resolve() == store or path.resolve().is_relative_to(store)
            or store.is_relative_to(path.resolve()) or linker.is_link(path)):
        raise ValueError("Payload view must be separate from the source store")
    config = _default_config_dir()
    config.mkdir(parents=True, exist_ok=True)
    with FileLock(config / "payload-views.lock", timeout=3, stale_after=None):
        registry_path, registry = _registry()
        entry = next((item for item in registry["views"] if Path(item["path"]) == path), None)
        if entry is None:
            if path.exists() or linker.is_link(path) or len(registry["views"]) >= 32:
                raise ValueError("Existing unregistered view path was preserved")
            entry = {"path": str(path), "store": str(cfg.effective_store_dir().resolve()),
                     "collection": collection, "identity": uuid.uuid4().hex}
            registry["views"].append(entry)
            _write(registry_path, registry)
        if entry["collection"] != collection:
            raise ValueError("Registered payload collection changed")
        return _refresh(cfg, entry, plan)


def refresh_payload_views(cfg: ClientConfig) -> list[dict]:
    """Refresh registered views after source updates, independently of agent copies."""
    config = _default_config_dir()
    if not (config / "payload-views.json").exists():
        return []
    with FileLock(config / "payload-views.lock", timeout=3, stale_after=None):
        _, registry = _registry()
        result = []
        for entry in registry["views"]:
            try:
                result.append({"accepted": True, **_refresh(cfg, entry)})
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                result.append({"accepted": False, "path": entry.get("path"),
                               "reason": str(exc), "error_type": type(exc).__name__})
        return result
