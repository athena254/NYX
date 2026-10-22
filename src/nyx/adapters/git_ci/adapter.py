"""The Git/CI adapter: slices 5.1 and 5.2 assembled behind the ``Adapter`` protocol.

``GitCiAdapter`` is the object ``api/app.py`` and ``cli/main.py`` construct when
``NYX_ENABLED_ADAPTERS`` names ``git-ci``. It holds **no state and no session**: it is a
read-only observer (ADR 0004) that turns GitHub's CI API into the producer *input* models the
ingestion gateway already validates, redacts, deduplicates, and audits. It never creates a
finding, a disposition, or a claim, and it imports nothing from the deterministic core — the
boundary that gate G7 (``tests/contract/test_adapter_boundary.py``) makes executable.

Three behaviours are the point of this module, and each is easy to get wrong:

* **An error is not an empty result.** ``fetch_for_ref`` lets :class:`GitCiError` propagate. A
  caller must be able to distinguish "the adapter is down" (an error, retry, do not conclude)
  from "the adapter saw nothing" (an empty list). There is no fallback provider and no silent
  second source: GitHub is unreachable, that is an error.
* **A payload with no usable records is ``[]``, not a fabricated record.** A run with no head
  SHA cannot be scoped, and ``scope.commit`` is the field C-001 correlates on, so it is refused
  rather than emitted with an empty scope. A run with no timestamp is refused, never defaulted
  to the fetch time (``HANDOFF.md`` §8). Malformed input raises a *typed* error naming the
  field — never a ``KeyError`` traceback from a missing key deep inside a normalizer.
* **``health()`` performs exactly one bounded ``GET`` and never raises.** One request, to the
  cheapest endpoint that proves the token, the host, and the repository are all usable. A
  failure or a non-2xx status is ``DEGRADED`` with a detail that carries no credential; an
  exception raised below the adapter is reported as ``DEGRADED`` with the exception's *type*
  name only, because a message can echo a request URL.

This module imports only ``nyx.adapters``, ``nyx.domain``, ``nyx.ingestion``, and the standard
library. It imports nothing from ``nyx.critics``, ``nyx.findings``, ``nyx.disputes``,
``nyx.policies``, ``nyx.storage``, ``nyx.retention``, ``nyx.auth``, ``nyx.systems``, or
``nyx.audit``.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from nyx.adapters.base import AdapterHealth, AdapterInput, AdapterState
from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.github_client import CODE_UNAVAILABLE, GitCiError, GitHubCiClient
from nyx.adapters.git_ci.normalize import (
    NormalizationError,
    normalize_actions_run,
    normalize_check_run,
)
from nyx.domain.base import utc_now

#: What this adapter is allowed to touch. It reads a repository's CI records; it writes nothing
#: anywhere, including to GitHub (ADR 0004). The capability names are NYX's own capability
#: vocabulary, not GitHub's, so the adapter's declared reach cannot be confused with a GitHub
#: OAuth scope.
PERMISSIONS: tuple[str, ...] = (
    "git.read",
    "ci.read",
)

#: What the adapter can observe: the CI records GitHub itself recorded. Each is an observation
#: of the host's record, not of the truth of any claim.
CAN_OBSERVE: tuple[str, ...] = (
    "GitHub check runs for the configured repository",
    "GitHub Actions workflow runs for the configured repository",
    "the head SHA, conclusion, and timestamps GitHub reported for those runs",
)

#: What the adapter cannot observe, stated so an empty result is interpretable rather than
#: ambiguous. An empty list from this adapter means "the host reported no completed runs",
#: never "the code is fine" and never "the claim is true".
CANNOT_OBSERVE: tuple[str, ...] = (
    "the truth of a claim; a CI record is correlated, not independent, corroboration",
    "the contents of the repository, the build artifacts, or any diff",
    "deployments, production state, or any system GitHub does not record as a CI run",
    "records from any provider other than the configured one; there is no fallback",
)

#: The one endpoint ``health()`` touches: the repository itself. It is cheaper than a runs
#: listing and it exercises the same three things — host reachability, the token, and the
#: configured repository — so a 2xx here is the smallest honest proof the adapter can fetch.
_HEALTH_PATH_TEMPLATE = "/repos/{repository}"

#: The ``detail`` for a successful probe. It states *what was checked*, not what was returned,
#: because a response body can carry anything.
_OK_DETAIL = "GitHub CI adapter reached the configured repository with the configured credentials"

#: Rendered when the probe could not be completed. The *type* of a failure is safe to report;
#: a message can echo a request URL that carries a credential.
_DEGRADED_TEMPLATE = "GitHub CI adapter health probe failed ({code}): {reason}"


class AdapterConfigurationError(ValueError):
    """The adapter was configured with a target it cannot observe.

    Raised when no repository is configured. It is a *typed* error rather than a
    ``TypeError`` on ``None`` deep inside the URL builder, so a caller can tell a
    misconfiguration apart from a fetch failure at the point of the call.
    """

    def __init__(self, message: str, *, field: str = "repository") -> None:
        super().__init__(message)
        self.field = field


def _http_client(client: GitHubCiClient) -> httpx.Client:
    """Return the ``httpx.Client`` a ``GitHubCiClient`` was built with.

    ``GitHubCiClient`` exposes no public accessor for the transport it was constructed with,
    and the adapter must issue its health probe through that same transport: a probe that
    opened its own connection would not be testing the client the adapter fetches with, and
    the injected transport a deployment uses would be bypassed.
    """

    return client._client


def _queryless(url: str) -> str:
    """Return ``url`` with its query string removed.

    The query string of a CI request carries the ref under observation; the path carries the
    repository. A detail string that names only the path names what was probed without naming
    the value being probed.
    """

    return url.split("?", 1)[0]


#: Fields whose absence means "the host did not report a usable observation" rather than "the
#: payload is malformed".
#:
#: The normalizer raises :class:`NormalizationError` naming the offending field. The adapter
#: turns an absent *scope* (``head_sha``) or *timestamp* into a skipped record — a CI record
#: with no commit cannot be scoped, and a record with no time cannot be ordered, so there is
#: nothing honest to emit. Every other refusal propagates: a missing ``id``, a missing ``name``,
#: or a value of the wrong type is a malformed payload, and a caller must be able to tell a
#: refused record from a broken one.
_SKIPPED_FIELDS = frozenset(
    {"head_sha", "completed_at", "started_at", "run_started_at", "updated_at"}
)


class GitCiAdapter:
    """Observes GitHub CI runs and emits producer inputs through the ingestion gateway.

    ``client`` is injectable so a caller can point the adapter at a local ``httpx``
    transport with no network — the same seam ``src/nyx/conversational/client.py`` provides.
    When it is not injected the adapter builds a :class:`GitHubCiClient` from ``config``.
    """

    adapter_id = "git-ci"
    version = "1.0.0"
    description = (
        "Observes GitHub check runs, Actions workflow runs, and commits as NYX events and CI "
        "evidence. Read-only: it performs GET requests against the configured repository and "
        "emits producer inputs, never findings, dispositions, or claims."
    )
    permissions = list(PERMISSIONS)
    can_observe = list(CAN_OBSERVE)
    cannot_observe = list(CANNOT_OBSERVE)

    def __init__(self, config: GitCiConfig, *, client: GitHubCiClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else GitHubCiClient(config)

    # -- Adapter protocol --------------------------------------------------

    def health(self) -> AdapterHealth:
        """Probe the adapter with exactly one bounded ``GET``. Never raises.

        ``OK`` on a 2xx response, ``DEGRADED`` on any failure or non-2xx status. The
        ``detail`` is composed from the URL *without its query string* and never from
        ``str(exception)``, which can carry the full request URL. A probe that itself raises
        is reported as ``DEGRADED`` with the exception's type name only.
        """

        checked_at = utc_now()
        request: httpx.Request | None = None
        try:
            path = _HEALTH_PATH_TEMPLATE.format(repository=self._config.repository)
            http_client = _http_client(self._client)
            request = http_client.build_request("GET", path)
            response = http_client.send(request)
        except GitCiError as exc:
            # The client already built the message from the status and the queryless URL.
            return self._degraded(exc.code, exc.args[0] if exc.args else "probe failed", checked_at)
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            return self._degraded(CODE_UNAVAILABLE, type(exc).__name__, checked_at)

        status = response.status_code
        if 200 <= status < 300:
            return AdapterHealth(
                adapter_id=self.adapter_id,
                version=self.version,
                state=AdapterState.OK,
                detail=_OK_DETAIL,
                checked_at=checked_at,
            )
        probed = (
            _queryless(str(request.url)) if request is not None else "the configured repository"
        )
        return self._degraded(
            CODE_UNAVAILABLE,
            f"health probe to {probed} returned HTTP {status}",
            checked_at,
        )

    def normalize(self, raw: object) -> list[AdapterInput]:
        """Turn one already-fetched GitHub payload into zero or more producer inputs.

        Dispatches on the payload's shape: ``check_runs`` items go through
        :func:`normalize_check_run`, ``workflow_runs`` items through
        :func:`normalize_actions_run`. A payload carrying no usable records returns ``[]``.

        **Two different refusals, deliberately distinguished.** A run the host left *incomplete*
        — no ``head_sha``, no timestamp — is skipped and contributes nothing: there is no
        honest record to emit, and a caller must be able to tell "the host reported nothing
        usable" from "the host is unreachable". A run the host left *malformed* — a missing
        ``id``, a missing ``name``, a value of the wrong type, a non-list runs key — raises a
        typed :class:`NormalizationError` naming the field, never a ``KeyError`` or
        ``AttributeError`` traceback. Silence on a malformed payload would let a broken record
        pass as an empty repository.
        """

        if not isinstance(raw, dict):
            raise NormalizationError("payload must be a JSON object", field="payload")

        inputs: list[AdapterInput] = []
        check_runs = raw.get("check_runs")
        if check_runs is not None and not isinstance(check_runs, list):
            raise NormalizationError("'check_runs' must be a list when present", field="check_runs")
        if isinstance(check_runs, list):
            for run in check_runs:
                try:
                    inputs.extend(normalize_check_run(run, repository=self._config.repository))
                except NormalizationError as exc:
                    # An absent scope or timestamp is "no usable record", not "malformed".
                    if exc.field not in _SKIPPED_FIELDS:
                        raise

        workflow_runs = raw.get("workflow_runs")
        if workflow_runs is not None and not isinstance(workflow_runs, list):
            raise NormalizationError(
                "'workflow_runs' must be a list when present", field="workflow_runs"
            )
        if isinstance(workflow_runs, list):
            for run in workflow_runs:
                try:
                    inputs.extend(normalize_actions_run(run, repository=self._config.repository))
                except NormalizationError as exc:
                    if exc.field not in _SKIPPED_FIELDS:
                        raise

        return inputs

    # -- observation -------------------------------------------------------

    def fetch_for_ref(self, ref: str) -> list[AdapterInput]:
        """Fetch and normalize the CI records for one git ref.

        Calls both endpoints, normalizes both responses, and returns the combined list. A
        :class:`GitCiError` from either fetch **propagates**: "the adapter is down" is an
        error a caller must retry, not an empty observation set. There is no fallback
        provider.
        """

        if not self._config.repository.strip():
            raise AdapterConfigurationError(
                "the git-ci adapter has no configured repository, so it has nothing to fetch; "
                "set git_ci_repository (owner/name) or do not enable the adapter",
                field="repository",
            )
        if not isinstance(ref, str) or not ref.strip():
            raise NormalizationError("ref must be a non-empty string", field="ref")

        inputs: list[AdapterInput] = []
        for run in self._client.fetch_check_runs(ref=ref):
            inputs.extend(normalize_check_run(run, repository=self._config.repository))
        for run in self._client.fetch_actions_runs(ref=ref):
            inputs.extend(normalize_actions_run(run, repository=self._config.repository))
        return inputs

    @property
    def config(self) -> GitCiConfig:
        """The adapter's effective configuration. Carries no credential by construction."""

        return self._config

    @property
    def client(self) -> GitHubCiClient:
        """The HTTP client this adapter fetches with, so a caller can read truncation."""

        return self._client

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _degraded(code: str, reason: str, checked_at: datetime) -> AdapterHealth:
        """Build a ``DEGRADED`` health with a detail that carries no credential.

        The reason is either the client's already queryless message or an exception *type
        name*. It is capped so a hostile detail cannot overflow the model's field limit.
        """

        return AdapterHealth(
            adapter_id=GitCiAdapter.adapter_id,
            version=GitCiAdapter.version,
            state=AdapterState.DEGRADED,
            detail=_DEGRADED_TEMPLATE.format(code=code, reason=reason)[:1000],
            checked_at=checked_at,
        )


__all__ = [
    "AdapterConfigurationError",
    "GitCiAdapter",
]
