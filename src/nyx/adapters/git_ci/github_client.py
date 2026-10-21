"""A bounded, read-only GitHub CI client (phase 5, slice 5.1).

The adapter observes GitHub; it never writes to it (ADR 0004). Every request this client makes
is a ``GET``, and there is no method on the class that can issue anything else.

Three rules this module exists to make enforceable:

* **The token never appears in an exception, a log, or a return value.** It is read from the
  named environment variable exactly once, in ``__init__``, and held in a ``SecretStr`` so it
  renders as ``**********`` in ``str()``/``repr()`` rather than as itself. The
  ``Authorization: Bearer <token>`` header is built once and attached to the ``httpx`` client,
  which is the only place the value exists in plain form.
* **Every failure is a :class:`GitCiError` carrying a stable code**, and its message is built
  from the HTTP status and the URL *without its query string*. It is never built from
  ``str(httpx_error)``, which can carry the full request URL — the defect ``HANDOFF.md`` §7
  records (a credential in ``base_url`` leaving in a header no test inspected).
* **A private target is refused before any request is made.** The same SSRF guard the evidence
  route applies at the ingestion edge is applied here, so the adapter's notion of "fetchable"
  cannot drift from the gateway's.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from nyx.adapters.git_ci.config import GitCiConfig
from nyx.ingestion.url_fetch import is_private_target

#: Stable failure codes. ``GitCiError.code`` is the branchable contract; the message is prose.
CODE_UNAVAILABLE = "unavailable"
CODE_UNAUTHORIZED = "unauthorized"
CODE_NOT_FOUND = "not_found"
CODE_INVALID_OUTPUT = "invalid_output"
CODE_DENIED = "denied"


class GitCiError(Exception):
    """A fetch failure. Carries a stable ``code``; never carries the token."""

    def __init__(self, message: str, *, code: str = CODE_UNAVAILABLE) -> None:
        super().__init__(message)
        self.code = code


class GitHubCiClient:
    """Read-only access to the GitHub check-runs and Actions-runs endpoints.

    ``transport`` is injectable so a test (or a future provider) can point the client at a local
    ``httpx`` transport with no network — the same seam as ``nyx.conversational.client``.
    """

    def __init__(
        self, config: GitCiConfig, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._config = config
        self._last_truncated = 0

        # Read once, here. A missing variable means "no Authorization header", and GitHub's
        # answer to that is a 401, which surfaces as `unauthorized` — the honest, observable
        # outcome rather than a KeyError at construction. The value is wrapped in a SecretStr
        # so it cannot render itself into a diagnostic.
        _token = os.environ.get(config.token_env) if config.token_env else None
        self._token: SecretStr | None = SecretStr(_token) if _token else None

        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"

        # The timeout is httpx's, taken from configuration: a client that can hang is a client
        # that can hang a request.
        self._client = httpx.Client(
            base_url=config.api_base_url,
            headers=headers,
            timeout=config.timeout_seconds,
            transport=transport,
        )

    # -- public surface ----------------------------------------------------

    @property
    def last_truncated(self) -> int:
        """How many records the most recent fetch dropped. ``0`` when nothing was dropped.

        A bounded fetch that is silent about what it dropped is an unreported truncation, so
        the count is exposed rather than logged and forgotten.
        """

        return self._last_truncated

    def close(self) -> None:
        """Release the underlying HTTP client's connection pool."""

        self._client.close()

    def fetch_check_runs(self, *, ref: str) -> list[dict[str, Any]]:
        """GET ``/repos/{repository}/commits/{ref}/check-runs`` and return the runs."""

        path = f"/repos/{self._config.repository}/commits/{quote(ref, safe='')}/check-runs"
        return self._fetch(path, key="check_runs", params=None)

    def fetch_actions_runs(self, *, ref: str) -> list[dict[str, Any]]:
        """GET ``/repos/{repository}/actions/runs?head_sha={ref}`` and return the runs."""

        path = f"/repos/{self._config.repository}/actions/runs"
        return self._fetch(path, key="workflow_runs", params={"head_sha": ref})

    # -- internals ---------------------------------------------------------

    def _fetch(
        self,
        path: str,
        *,
        key: str,
        params: dict[str, str] | None,
    ) -> list[dict[str, Any]]:
        # The SSRF guard runs before the request exists, so a refused target leaves no request
        # behind to be recorded by a transport or an audit log.
        self._guard_target()

        # Build the request first so the message below can name the URL this call actually
        # uses, with the query string removed — the query can carry a ref or a token.
        request = self._client.build_request("GET", path, params=params)
        safe_url = _without_query(request.url)

        try:
            response = self._client.send(request)
        except httpx.TimeoutException as exc:
            raise GitCiError(
                f"GitHub CI request to {safe_url} timed out", code=CODE_UNAVAILABLE
            ) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # `type(exc).__name__` is safe; `str(exc)` is not, because httpx can include the
            # full request URL (with its query string) in a transport error.
            raise GitCiError(
                f"GitHub CI request to {safe_url} did not reach the host ({type(exc).__name__})",
                code=CODE_UNAVAILABLE,
            ) from exc

        if response.status_code == 401:
            raise GitCiError(
                f"GitHub CI request to {safe_url} returned HTTP 401", code=CODE_UNAUTHORIZED
            )
        if response.status_code == 404:
            raise GitCiError(
                f"GitHub CI request to {safe_url} returned HTTP 404", code=CODE_NOT_FOUND
            )
        if response.status_code == 403:
            raise GitCiError(f"GitHub CI request to {safe_url} returned HTTP 403", code=CODE_DENIED)
        if response.status_code == 429 or response.status_code >= 400:
            raise GitCiError(
                f"GitHub CI request to {safe_url} returned HTTP {response.status_code}",
                code=CODE_UNAVAILABLE,
            )

        try:
            payload: object = response.json()
        except ValueError as exc:
            raise GitCiError(
                f"GitHub CI response from {safe_url} was not JSON", code=CODE_INVALID_OUTPUT
            ) from exc

        runs = _extract(payload, key, safe_url)
        limit = self._config.max_runs_per_fetch
        self._last_truncated = max(0, len(runs) - limit)
        return runs[:limit]

    def _guard_target(self) -> None:
        """Refuse a private or non-routable host before any request is made."""

        if self._config.allow_private_targets:
            return
        try:
            host = urlsplit(self._config.api_base_url).hostname
        except ValueError:
            # A malformed base URL has no hostname to resolve; refusing it is the safe answer,
            # and the request itself would fail anyway.
            host = None
        if host and is_private_target(host):
            raise GitCiError(
                f"refusing to fetch host {host!r}: it resolves to a private or non-routable "
                "target; set allow_private_targets to override",
                code=CODE_DENIED,
            )


# --------------------------------------------------------------------------- helpers


def _without_query(url: httpx.URL) -> str:
    """Render ``url`` without its query string or fragment.

    A query string is where a credential or a ref would ride into a message that gets logged,
    so the message is composed from the path-bearing form only.
    """

    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _extract(payload: object, key: str, safe_url: str) -> list[dict[str, Any]]:
    """Pull the ``key`` list out of a GitHub payload, or fail as ``invalid_output``."""

    if not isinstance(payload, dict):
        raise GitCiError(
            f"GitHub CI response from {safe_url} was not a JSON object",
            code=CODE_INVALID_OUTPUT,
        )
    items = payload.get(key)
    if not isinstance(items, list):
        raise GitCiError(
            f"GitHub CI response from {safe_url} carried no '{key}' list",
            code=CODE_INVALID_OUTPUT,
        )
    return [item for item in items if isinstance(item, dict)]


__all__ = [
    "CODE_DENIED",
    "CODE_INVALID_OUTPUT",
    "CODE_NOT_FOUND",
    "CODE_UNAUTHORIZED",
    "CODE_UNAVAILABLE",
    "GitCiError",
    "GitHubCiClient",
]
