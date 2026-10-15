"""Share materialized instructions through the public SDK ownership gates."""

from __future__ import annotations

from pathlib import Path

from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import get_target
from skillery_cli.core.installer import SkillInstaller


def resolve_agent_targets(agents: str | None, agent: str | None, default: str) -> list:
    """Resolve every target before a collection can mutate its first scope."""
    if agents is not None and agent is not None:
        raise ValueError("--agents and --agent are mutually exclusive")
    names = list(dict.fromkeys(name.strip() for name in agents.split(",") if name.strip())) if agents is not None else [agent or default]
    if not names:
        raise ValueError("--agents must not be empty")
    return [get_target(name) for name in names]


def link_installed_chain(cfg: ClientConfig, chain: list[dict], *, targets: list,
                         project: Path | None) -> tuple[list[dict], list[dict]]:
    """Link visible releases without another download, login, hook or MCP edit.

    Tooling is installed by the primary installation. This operation exposes
    the same instructions to other agents and refuses edited or foreign paths.
    """
    installed, refused = [], []
    for target in targets:
        installer = SkillInstaller(target, cfg.effective_store_dir(), projection=cfg.skill_projection)
        for item in chain:
            if item.get("skipped") or item.get("agent_visible") is False:
                continue
            slug = item.get("slug") or item.get("skill_id")
            if not slug:
                continue
            try:
                result = installer.link_existing(str(slug), project=project, force=False,
                                                 projection=cfg.skill_projection)
                if result is None:
                    raise ValueError("Materialized release is missing from the store")
                linked, kind = result
                summary = {key: item[key] for key in (
                    "slug", "skill_id", "version", "scope", "install_reason", "agent_visible",
                ) if key in item}
                installed.append({**summary, "agent": target.name, "source": "store",
                                  "is_update": False,
                                  "store_dir": str(cfg.effective_store_dir() / str(slug)),
                                  "target_dir": str(target.slug_dir(str(slug), project=project)),
                                  "linked": linked, "link_kind": kind})
            except (OSError, RuntimeError, ValueError) as exc:
                refused.append({"ref": str(slug), "agent": target.name,
                                "reason": getattr(exc, "reason", "scope-link-refused"),
                                "error": str(exc)})
    return installed, refused
