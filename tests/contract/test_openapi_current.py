"""The checked-in OpenAPI document must be regenerated whenever the API changes."""

from __future__ import annotations

from nyx.api.openapi import openapi_path, render_openapi


def test_checked_in_openapi_is_current() -> None:
    checked_in = openapi_path().read_text(encoding="utf-8")

    assert checked_in == render_openapi(), (
        "docs/openapi.json is stale; run "
        '`uv run python -c "from nyx.api.openapi import write_openapi; write_openapi()"`'
    )
