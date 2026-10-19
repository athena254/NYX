"""Render the OpenAPI document the same way the drift test compares it.

The checked-in document at ``docs/openapi.json`` is generated from the live app. The contract
test regenerates it and fails if it differs, so the document cannot silently drift.
"""

from __future__ import annotations

import json
from pathlib import Path

from nyx.api.app import create_app
from nyx.storage.migrations import repository_root


def openapi_path() -> Path:
    """Return the checked-in OpenAPI document path."""

    return repository_root() / "docs" / "openapi.json"


def render_openapi() -> str:
    """Return the deterministic JSON rendering of the application's OpenAPI document."""

    app = create_app()
    document = app.openapi()
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_openapi(path: Path | None = None) -> Path:
    """Write the current OpenAPI document to ``path`` (default: the checked-in file)."""

    target = path or openapi_path()
    target.write_text(render_openapi(), encoding="utf-8")
    return target
