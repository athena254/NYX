"""A fixture CI provider (phase 5, slice 5.5): the offline Git/CI source.

``FixtureCiClient`` presents the same surface as
:class:`nyx.adapters.git_ci.github_client.GitHubCiClient` — ``fetch_check_runs(ref=...)`` and
``fetch_actions_runs(ref=...)`` returning ``list[dict[str, Any]]``, plus the ``last_truncated``
property — but reads a **checked-in JSON file** instead of the GitHub API. That is the offline
path: no network, no git, no token, and no filesystem traversal beyond the single file the caller
names. It is what makes the adapter usable in CI and in the demo, and it is what slice 5.5's
disablement test drives the adapter with.

**What the file is.** A check-runs fixture is a decoded GitHub response body:
``{"total_count": N, "check_runs": [...]}``. An Actions fixture is
``{"total_count": N, "workflow_runs": [...]}``. The payloads are deliberately *the same shape the
host returns*, so ``normalize.py`` does not change and neither does the adapter — the provider is
the only thing that differs.

**The ref filter.** A fixture stands in for a whole repository, so a fetch is a filter, not a
lookup: an item whose ``ref`` differs from the requested one is left out, and an item with no
``ref`` field at all is treated as matching (a fixture that does not model branches yields
everything). A filter that matches nothing is ``[]`` — an empty result is not an error.

**Failures are typed, never a traceback.** A path that is missing, a path that is ``None`` when the
provider needs it, a file that is not JSON, and a payload with no ``check_runs`` /
``workflow_runs`` list all raise :class:`GitCiError` with the stable code the other two providers
use — ``not_found`` for a missing path or file, ``invalid_output`` for anything unreadable. A bare
``FileNotFoundError`` or ``JSONDecodeError`` escaping to a caller would be a diagnostic with no
branchable code, which is the whole reason the codes exist.

**Truncation is reported.** ``max_runs_per_fetch`` bounds the result and ``last_truncated`` counts
what the bound dropped, exactly as the GitHub and local-git providers do. A bounded fetch that is
silent about what it dropped is an unreported truncation.

This module imports only its own sibling ``config`` / ``github_client`` plus the standard library
(the adapter boundary, ``docs/architecture.md`` §3). It imports nothing from ``nyx.critics``,
``nyx.findings``, ``nyx.disputes``, ``nyx.policies``, ``nyx.storage``, ``nyx.retention``,
``nyx.auth``, ``nyx.systems``, or ``nyx.audit``, and it starts no process and touches no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.github_client import (
    CODE_INVALID_OUTPUT,
    CODE_NOT_FOUND,
    GitCiError,
)

#: The list key each payload must carry, in the vocabulary the GitHub API itself uses.
_CHECK_RUNS_KEY = "check_runs"
_ACTIONS_RUNS_KEY = "workflow_runs"


def _missing_list_error(path: Path | None, *, key: str) -> GitCiError:
    """The ``invalid_output`` error for a payload with no ``key`` list."""

    location = path.as_posix() if path is not None else "the fixture"
    return GitCiError(
        f"the fixture CI payload at {location!r} carries no '{key}' list",
        code=CODE_INVALID_OUTPUT,
    )


#: How far a single fixture read may go. A fixture is checked-in source, but a multi-gigabyte
#: file is a mistake the provider should refuse rather than load into memory.
_MAX_FIXTURE_BYTES = 1_048_576


class FixtureCiClient:
    """Same interface as ``GitHubCiClient``, reading a checked-in JSON fixture.

    ``check_runs_path`` and ``actions_runs_path`` are absolute paths to the fixture files. A
    ``None`` path is not a lazy default: fetching that vocabulary raises ``not_found``, because a
    provider that silently returns nothing for a file it was never given would report an empty
    repository where there is no repository at all.
    """

    def __init__(
        self,
        config: GitCiConfig,
        *,
        check_runs_path: Path | None = None,
        actions_runs_path: Path | None = None,
    ) -> None:
        self._config = config
        self._check_runs_path = check_runs_path
        self._actions_runs_path = actions_runs_path
        self._last_truncated = 0

    # -- public surface ----------------------------------------------------

    @property
    def last_truncated(self) -> int:
        """How many records the most recent fetch dropped. ``0`` when nothing was dropped."""

        return self._last_truncated

    def close(self) -> None:
        """Interface parity with ``GitHubCiClient``; there is no connection to release."""

        return None

    def fetch_check_runs(self, *, ref: str) -> list[dict[str, Any]]:
        """The check runs in the fixture whose ``ref`` matches, bounded by configuration."""

        return self._fetch(self._check_runs_path, key=_CHECK_RUNS_KEY, ref=ref)

    def fetch_actions_runs(self, *, ref: str) -> list[dict[str, Any]]:
        """The Actions runs in the fixture whose ``ref`` matches, bounded by configuration."""

        return self._fetch(self._actions_runs_path, key=_ACTIONS_RUNS_KEY, ref=ref)

    # -- internals ---------------------------------------------------------

    def _fetch(self, path: Path | None, *, key: str, ref: str) -> list[dict[str, Any]]:
        payload = self._load(path, key=key)
        items = payload.get(key)
        if not isinstance(items, list):
            raise _missing_list_error(path, key=key)
        runs = self._matching(items, ref=ref)
        limit = self._config.max_runs_per_fetch
        self._last_truncated = max(0, len(runs) - limit)
        return runs[:limit]

    def _load(self, path: Path | None, *, key: str) -> dict[str, Any]:
        """Read one fixture and return its decoded object, or fail with a stable code."""

        if path is None:
            raise GitCiError(
                "the fixture CI provider was given no fixture path, so there is nothing to read",
                code=CODE_NOT_FOUND,
            )
        if not path.is_file():
            # The message names the caller-supplied location, not a traceback: this is a
            # configuration fact (a wrong path), not an accident to be debugged.
            raise GitCiError(
                f"the fixture CI file {path.as_posix()!r} does not exist",
                code=CODE_NOT_FOUND,
            )

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GitCiError(
                f"the fixture CI file {path.as_posix()!r} could not be read",
                code=CODE_NOT_FOUND,
            ) from exc

        if len(text.encode("utf-8")) > _MAX_FIXTURE_BYTES:
            raise GitCiError(
                f"the fixture CI file {path.as_posix()!r} is larger than "
                f"{_MAX_FIXTURE_BYTES} bytes",
                code=CODE_INVALID_OUTPUT,
            )

        try:
            payload: object = json.loads(text)
        except ValueError as exc:
            raise GitCiError(
                f"the fixture CI file {path.as_posix()!r} is not valid JSON",
                code=CODE_INVALID_OUTPUT,
            ) from exc

        if not isinstance(payload, dict):
            raise GitCiError(
                f"the fixture CI file {path.as_posix()!r} is not a JSON object",
                code=CODE_INVALID_OUTPUT,
            )
        return payload

    @staticmethod
    def _matching(items: list[Any], *, ref: str) -> list[dict[str, Any]]:
        """Keep the fixture items whose ``ref`` matches, or all of them when none is modelled.

        An item that is not an object is left out rather than raising: a fixture is checked-in
        source, and one malformed entry should not make the whole repository unobservable. The
        normalizer is the layer that refuses a malformed *run*.
        """

        runs = [item for item in items if isinstance(item, dict)]
        return [run for run in runs if FixtureCiClient._ref_matches(run, ref=ref)]

    @staticmethod
    def _ref_matches(run: dict[str, Any], *, ref: str) -> bool:
        """Whether one fixture item belongs to the requested ref.

        A fixture with no ``ref`` field models a repository that does not distinguish branches,
        so every item matches. With one, the filter is exact — a fixture is a stand-in for the
        whole repository, and returning another ref's runs would be reporting observations that
        were never made for the ref under assessment.
        """

        item_ref = run.get("ref")
        if item_ref is None:
            return True
        return str(item_ref) == ref


__all__ = ["FixtureCiClient"]
