"""A thin HTTP client for the NYX API.

This is the **only** path the conversational shell has to NYX core. It exists so the shell can never
reach into the deterministic modules -- it talks to NYX the way any other system would, over HTTP.

Two consequences that matter:

* **The shell cannot do anything a human cannot do through the API.** Whatever the API refuses, the
  shell cannot do. That is the mechanism behind "the language model is the interface, not the
  judge": a tool surface built on this client cannot create a finding or record a disposition,
  because those endpoints are not offered here.
* **Structured errors pass through.** NYX returns ``{"code", "message", "request_id"}``; this client
  raises :class:`NyxClientError` carrying that code, so a caller can distinguish "not found" from
  "capability denied" without parsing prose.

The client is deliberately read-mostly. The only writes it exposes are filing a rebuttal, requesting
verification, and running queued verification -- the same things a user can do by hand.
"""

from __future__ import annotations

from typing import Any

import httpx

from nyx.domain.enums import RebuttalRequestedOutcome

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0


class NyxClientError(Exception):
    """An NYX API call failed. Carries NYX's stable error ``code``."""

    def __init__(self, message: str, *, code: str = "INTERNAL_ERROR", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class NyxUnavailableError(NyxClientError):
    """The NYX service could not be reached at all."""


class NyxClient:
    """Read and limited-write access to a running NYX service."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    # -- internals ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.request(method, self._url(path), params=params, json=json)
        except httpx.TimeoutException as exc:
            raise NyxUnavailableError(f"NYX at {self._base_url} timed out", code="TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise NyxUnavailableError(
                f"NYX at {self._base_url} is unreachable ({type(exc).__name__})",
                code="UNAVAILABLE",
            ) from exc

        if response.status_code == 404:
            raise NyxClientError("not found", code="NOT_FOUND", status=404)
        if response.status_code >= 400:
            raise _api_error(response)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise NyxClientError(
                "NYX returned a response that is not JSON", code="INVALID_OUTPUT"
            ) from exc

    # -- systems -----------------------------------------------------------

    def list_systems(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List registered monitored systems."""

        payload = self._request("GET", "/api/v1/systems", params={"limit": limit})
        return _items(payload)

    # -- findings ----------------------------------------------------------

    def list_findings(
        self,
        *,
        status: str | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List findings, optionally filtered by status or severity."""

        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        if severity is not None:
            params["severity"] = severity
        payload = self._request("GET", "/api/v1/findings", params=params)
        return _items(payload)

    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        """Return one finding with its evidence links, or ``None`` if absent."""

        try:
            return _as_dict(self._request("GET", f"/api/v1/findings/{finding_id}"))
        except NyxClientError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise

    def list_critics(self) -> list[dict[str, Any]]:
        """List the registered critics and their declared scope."""

        payload = self._request("GET", "/api/v1/critics")
        items = _items(payload)
        return [item for item in items if isinstance(item, dict)]

    # -- dispute history ---------------------------------------------------

    def get_history(self, finding_id: str) -> dict[str, Any]:
        """Return the full dispute record: finding, rebuttals, verification, dispositions.

        Nothing is omitted -- every record is append-only, so the history is complete.
        """

        return _as_dict(self._request("GET", f"/api/v1/findings/{finding_id}/history"))

    def list_rebuttals(self, finding_id: str) -> list[dict[str, Any]]:
        """List a finding's rebuttals, oldest first."""

        payload = self._request("GET", f"/api/v1/findings/{finding_id}/rebuttals")
        return _items(payload)

    def list_verification_jobs(self, finding_id: str) -> list[dict[str, Any]]:
        """List a finding's verification attempts, oldest first."""

        payload = self._request("GET", f"/api/v1/findings/{finding_id}/verification-jobs")
        return _items(payload)

    def get_verification_job(self, job_id: str) -> dict[str, Any] | None:
        """Return one verification attempt, or ``None`` if absent."""

        try:
            return _as_dict(self._request("GET", f"/api/v1/verification-jobs/{job_id}"))
        except NyxClientError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise

    def list_policy_versions(self, *, rule_id: str | None = None) -> list[dict[str, Any]]:
        """List recorded policy changes, newest first."""

        params = {"rule_id": rule_id} if rule_id is not None else None
        payload = self._request("GET", "/api/v1/policies/versions", params=params)
        return _items(payload)

    # -- writes (the only mutations this client offers) --------------------

    def submit_rebuttal(
        self,
        *,
        finding_id: str,
        submitted_by: str,
        argument: str,
        evidence_ids: list[str] | None = None,
        requested_outcome: RebuttalRequestedOutcome | str = RebuttalRequestedOutcome.NO_CHANGE,
    ) -> dict[str, Any]:
        """File a challenge. The rebuttal appends; the finding is not overwritten."""

        outcome = (
            requested_outcome if isinstance(requested_outcome, str) else requested_outcome.value
        )
        return _as_dict(
            self._request(
                "POST",
                f"/api/v1/findings/{finding_id}/rebuttals",
                json={
                    "submitted_by": submitted_by,
                    "argument": argument,
                    "evidence_ids": list(evidence_ids or []),
                    "requested_outcome": outcome,
                },
            )
        )

    def request_verification(
        self,
        *,
        finding_id: str,
        check_id: str,
        requested_by: str,
    ) -> dict[str, Any]:
        """Queue an allowlisted verification check.

        A check that is not on the allowlist is refused by NYX before anything executes, and that
        refusal surfaces here as ``VERIFICATION_NOT_PERMITTED``.
        """

        return _as_dict(
            self._request(
                "POST",
                f"/api/v1/findings/{finding_id}/verification-jobs",
                json={"check_id": check_id, "requested_by": requested_by},
            )
        )

    def run_verification(self, *, finding_id: str) -> list[dict[str, Any]]:
        """Run the finding's queued verification jobs through the allowlist.

        A job that fails or is refused is recorded as such by NYX, never rewritten into a success.
        """

        payload = self._request("POST", f"/api/v1/findings/{finding_id}/verification-jobs/run")
        return _items(payload)


# --------------------------------------------------------------------------- helpers


def _as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NyxClientError("NYX returned an unexpected payload", code="INVALID_OUTPUT")
    return value


def _items(payload: Any) -> list[dict[str, Any]]:
    """Extract the ``items`` array from a page, tolerating a bare array."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        raw = payload.get("items")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    raise NyxClientError("NYX returned an unexpected page payload", code="INVALID_OUTPUT")


def _api_error(response: httpx.Response) -> NyxClientError:
    """Convert a non-2xx NYX response into a typed error carrying NYX's own code."""

    code = "INTERNAL_ERROR"
    message = f"NYX returned HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        raw_message = payload.get("message")
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
    return NyxClientError(message, code=code, status=response.status_code)
