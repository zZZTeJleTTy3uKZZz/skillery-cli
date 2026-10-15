"""CLI operations for payload-only hard-link views intended for read-only mounts."""
from pathlib import Path
from typing import Annotated

import typer

from skillery_cli.config import ClientConfig
from skillery_cli.core.payload_views import expose_payload_view, refresh_payload_views
from skillery_cli.output import emit_data, emit_error


def expose(
    collection: Annotated[str, typer.Option("--collection", help="An explicit local collection.")],
    path: Annotated[Path | None, typer.Option("--path", help="Target on the same filesystem as the store.")] = None,
) -> None:
    """Expose released files using hard links; exclude credentials and caches."""
    cfg = ClientConfig.load()
    target = path or cfg.effective_store_dir().parent / "payloads" / collection
    try:
        result = expose_payload_view(cfg, collection=collection, path=target)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit_error("PAYLOAD_VIEW_REFUSED", str(exc))
        raise typer.Exit(1) from exc
    emit_data({"event": "payload_view_exposed", **result})


def refresh() -> None:
    """Refresh registered payload views through their ownership and byte checks."""
    try:
        results = refresh_payload_views(ClientConfig.load())
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit_error("PAYLOAD_VIEW_REFUSED", str(exc))
        raise typer.Exit(1) from exc
    emit_data({"event": "payload_views_refreshed", "views": results})
    if any(not item["accepted"] for item in results):
        raise typer.Exit(1)
