"""Converge existing owned scopes through the public SDK projection gates."""

from __future__ import annotations

import json
from pathlib import Path

from librarykit.config_util import atomic_write_text
from skillkit import linker
from skillkit.errors import ScopeConflict
from skillkit.installer import is_managed_dir
from skillkit.targets.base import BaseAgentTarget

from skillery_cli.config import ClientConfig, _default_config_dir
from skillery_cli.core import project_manifest
from skillery_cli.core.agents import get_target
from skillery_cli.core.installer import SkillInstaller, read_meta

_MAX_PROJECTS = 256
_MAX_REGISTRY_BYTES = 65536


def _registry_path() -> Path:
    return _default_config_dir() / "managed-scopes.json"


def registered_project_roots() -> list[Path]:
    """Read an explicit profile-local registry without discovering repositories."""
    path = _registry_path()
    if not path.exists():
        return []
    if linker.is_link(path) or path.stat().st_size > _MAX_REGISTRY_BYTES:
        raise ValueError("Invalid managed scope registry")
    data = json.loads(path.read_text(encoding="utf-8"))
    roots = data.get("projects") if isinstance(data, dict) and data.get("schema") == 1 else None
    if not isinstance(roots, list) or len(roots) > _MAX_PROJECTS:
        raise ValueError("Invalid managed scope registry")
    result: list[Path] = []
    for root in roots:
        if not isinstance(root, str) or len(root) > 4096 or "\x00" in root:
            raise ValueError("Invalid managed project root")
        project = Path(root)
        if not project.is_absolute():
            raise ValueError("Managed project roots must be absolute")
        if project not in result:
            result.append(project)
    return result


def register_project_scope(project: Path) -> None:
    """Register only after a requested project install/enable succeeds."""
    project = Path(project).resolve()
    if not project_manifest.load(project):
        return
    projects = registered_project_roots()
    if project in projects:
        return
    if len(projects) >= _MAX_PROJECTS:
        raise ValueError("Managed scope registry limit reached")
    projects.append(project)
    payload = json.dumps({"schema": 1, "projects": [str(root) for root in projects]}, indent=2)
    if len(payload.encode()) > _MAX_REGISTRY_BYTES:
        raise ValueError("Managed scope registry size limit reached")
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, payload + "\n", encoding="utf-8")


class _ExistingScopeTarget(BaseAgentTarget):
    """Address one declared existing layout through the normal SDK copy gate."""

    def __init__(self, target, base: Path) -> None:
        super().__init__(root=base.parent)
        self.name = target.name
        self._preserved = target.preserved_paths()

    def base_dir(self, *, project: Path | None = None) -> Path:
        return self._root / "skills"

    def preserved_paths(self) -> tuple[str, ...]:
        return self._preserved


def reconcile_managed_scopes(cfg: ClientConfig, *, projects: tuple[Path, ...] = ()) -> dict:
    """Never create missing scopes, claim foreign paths, or install tooling/MCP."""
    report: dict = {"updated": [], "unchanged": [], "refused": [], "partial": False}
    names = cfg.sync_agents or [get_target(cfg.agent).name]
    scopes: list[Path | None] = [None]
    try:
        registered = registered_project_roots()
    except (OSError, ValueError) as exc:
        report["refused"].append({"reason": "invalid-scope-registry", "error": type(exc).__name__})
        registered = []
    for project in (*registered, *projects):
        if project not in scopes:
            scopes.append(project)
    store = cfg.effective_store_dir()
    for name in names:
        target = get_target(name)
        for project in scopes:
            bases = (target.base_dir(project=project),
                     *target.legacy_base_dirs(project=project))
            for base in dict.fromkeys(bases):
                if not base.is_dir() or linker.is_link(base):
                    continue
                try:
                    declared = project_manifest.load(project) if project is not None else None
                except (OSError, ValueError):
                    report["refused"].append({"agent": name, "project": str(project),
                                              "reason": "invalid-project-manifest"})
                    continue
                installer = SkillInstaller(
                    _ExistingScopeTarget(target, base), store, projection=cfg.skill_projection,
                )
                for slot in sorted(base.iterdir(), key=lambda entry: entry.name):
                    entry = {"agent": name, "skill": slot.name, "path": str(slot),
                             "project": str(project) if project is not None else None}
                    source = store / slot.name
                    if linker.is_link(source) or not source.is_dir() or not is_managed_dir(source):
                        continue
                    try:
                        linked_scope = linker.is_link(slot)
                        before = None if linked_scope else read_meta(slot)
                        source_meta = read_meta(source) or {}
                        ref = source_meta.get("slug") or str(source_meta.get("skill_id") or slot.name)
                        if declared is not None and ref not in declared:
                            continue
                        if not linked_scope and not is_managed_dir(slot):
                            entry["reason"] = "unmanaged-scope"
                            report["refused"].append(entry)
                            continue
                        # The SDK validates ownership, store binding, and BOTH byte baselines.
                        result = installer.link_existing(slot.name, project=project, force=False)
                        if result is None:
                            continue
                        after = None if linker.is_link(slot) else read_meta(slot)
                        changed = before != after
                        report["updated" if changed else "unchanged"].append(entry)
                    except (ScopeConflict, OSError, ValueError) as exc:
                        entry["reason"] = getattr(exc, "reason", type(exc).__name__)
                        entry["conflict_paths"] = list(getattr(exc, "paths", ()))
                        report["refused"].append(entry)
    # Store progress and individual scope refusals are independent transactions.
    report["partial"] = bool(report["refused"] and (report["updated"] or report["unchanged"]))
    return report


def attach_scope_report(cfg: ClientConfig, chain: list[dict], *, project: Path | None) -> None:
    """Expose independent scope results without marking source updates as failures."""
    if not cfg.sync_agents or not any(not item.get("skipped") for item in chain):
        return
    report = reconcile_managed_scopes(cfg, projects=(project,) if project is not None else ())
    report["partial"] = bool(report["refused"])
    for item in chain:
        if not item.get("skipped"):
            item["scope_reconciliation"] = report
            item["partial"] = report["partial"]
            break
