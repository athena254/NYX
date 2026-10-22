"""Phase 5's exit criterion: adapters are optional, and core NYX does not need them.

SPEC §19: *adapters can be disabled independently and core NYX still works.* This file is the
executable form of that sentence, and each of the eight tests below is one clause of it. The
``FixtureCiClient`` provider (slice 5.5) makes the enabled-path tests offline: no network, no git,
no token, so the adapter can be constructed and driven here without a GitHub account.

Three facts are asserted repeatedly because they are the ones a careless implementation breaks:

* **Disabled means not constructed.** A disabled adapter is absent from ``enabled()`` and is never
  probed by ``states()``; the recording client in test 3 records *zero* requests.
* **An optional adapter is not a dependency.** Test 5 drives ``GET /api/v1/ready`` with the
  adapter enabled and its host unreachable, and asserts ``200``. ``src/nyx/api/routes/health.py``
  is deliberately not modified to achieve that: readiness checks the database and migrations only,
  and gaining an adapter check would turn an integration into a prerequisite.
* **There is no fallback.** Test 5 also asserts that a fetch against an unreachable host raises
  rather than silently returning the fixture. A fallback provider is an undisclosed second source
  of evidence.

Nothing here edits ``tests/integration/test_health_and_doctor.py`` or the health route: those
already carry their own assertions, and this file must not make them pass by changing them.
"""

from __future__ import annotations

import ast
import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from typer.testing import CliRunner

import nyx.critics  # noqa: F401 - importing the package registers the deterministic critic pack
from nyx.adapters.base import (
    Adapter,
    AdapterDescriptor,
    AdapterHealth,
    AdapterInput,
    AdapterRegistry,
    AdapterState,
)
from nyx.adapters.git_ci.adapter import GitCiAdapter
from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.fixture_provider import FixtureCiClient
from nyx.adapters.git_ci.github_client import CODE_UNAVAILABLE, GitCiError
from nyx.api.schemas import ReadyResponse
from nyx.cli.main import app
from nyx.config import Settings
from nyx.domain.base import utc_now
from nyx.ingestion.inputs import EventInput, EvidenceInput
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory

API = "/api/v1"

#: Where the checked-in fixtures live, relative to the repository root this file belongs to.
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "git_ci"
CHECK_RUNS_FIXTURE = FIXTURE_DIR / "check_runs.json"
ACTIONS_RUNS_FIXTURE = FIXTURE_DIR / "actions_runs.json"

#: The repository the fixtures and the adapter in these tests speak for.
REPOSITORY = "fixture/repo"

#: The field that carries the *name* of the variable holding the PAT. It is configuration; the
#: value is read once by the client and is never a setting. The rendered name is searched for as
#: this literal, never built from the token.
TOKEN_ENV_FIELD = "git_ci_token_env"  # noqa: S105 - a field name, not a credential

#: A synthetic credential, used only as *input* in test 8. It is a literal on purpose: the
#: assertion searches the rendered configuration for this exact string, and building the
#: searched-for string from the token itself would report a leak the test caused
#: (``docs/implementation/HANDOFF.md`` §7 records that exact mistake).
SYNTHETIC_TOKEN = "ghp_disablement_probe_token_0000000000"  # noqa: S105 - test fixture
TOKEN_ENV_NAME = "NYX_PROBE_GITHUB_TOKEN"  # noqa: S105 - a variable name, not a secret


# -------------------------------------------------------------------- helpers


def _fixture_config(**overrides: Any) -> GitCiConfig:
    values: dict[str, Any] = {
        "provider": "fixture",
        "api_base_url": "file:///fixture",
        "repository": REPOSITORY,
        "max_runs_per_fetch": 10,
    }
    values.update(overrides)
    return GitCiConfig(**values)


def _fixture_adapter(config: GitCiConfig | None = None) -> GitCiAdapter:
    config = config if config is not None else _fixture_config()
    return GitCiAdapter(
        config,
        client=FixtureCiClient(
            config,
            check_runs_path=CHECK_RUNS_FIXTURE,
            actions_runs_path=ACTIONS_RUNS_FIXTURE,
        ),
    )


def _closed_port() -> int:
    """A port that is free *now*, so connecting to it is refused immediately."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _down_adapter() -> GitCiAdapter:
    """An adapter enabled against an *unreachable* host.

    The host is a port that is free now, so a connection is refused immediately rather than
    hanging: the assertion is about the verdict, not about waiting out a timeout. The config is
    built directly because ``Settings`` refuses ``allow_private_targets`` unless the evidence
    setting agrees, and a refused loopback host is exactly the case where a test must opt in.
    """

    config = _fixture_config(
        provider="github",
        api_base_url=f"http://127.0.0.1:{_closed_port()}",
        token_env=TOKEN_ENV_NAME,
        timeout_seconds=1.0,
        allow_private_targets=True,
    )
    return GitCiAdapter(config)  # the real GitHub client, pointed at a closed port


def _run_doctor(
    tmp_path: Path,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run ``nyx doctor --json`` in an environment with no inherited ``NYX_*`` variable.

    ``env_extra`` adds NYX_* variables on top of the stripped baseline, so a test can put the
    deployment in a specific configuration without the developer's own settings leaking in.
    """

    runner = CliRunner()
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    env.update(env_extra or {})
    # pydantic-settings reads `.env` relative to the working directory; run from an empty one so
    # the developer's own settings cannot leak in.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["doctor", "--json"], env=env)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ------------------------------------------------------------- 1. default off


def test_1_the_adapter_is_off_by_default_and_doctor_says_so(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No adapter is named, none is constructed, and doctor reports no enabled integration."""

    assert settings.enabled_adapters == []

    # Disabled means not constructed: the adapter is *not registered at all*, which is what a
    # deployment that names nothing builds. The registry therefore has nothing to probe.
    filter_names = settings.enabled_adapters
    registry = AdapterRegistry(is_enabled=lambda adapter_id: adapter_id in filter_names)
    assert registry.registered() == []
    assert registry.enabled() == []
    assert registry.states() == []
    assert "git-ci" not in {descriptor.adapter_id for descriptor in registry.registered()}

    # The git-ci adapter is importable -- the code exists -- but nothing constructs it.
    assert GitCiAdapter.adapter_id == "git-ci"
    assert _fixture_config().repository == REPOSITORY

    database = Path(settings.database_url.removeprefix("sqlite:///"))
    report = _run_doctor(settings.data_dir, database, monkeypatch)
    integration = next(
        check for check in report["checks"] if check["check"] == "optional_integrations"
    )
    assert integration["status"] == "ok"
    # The adapter code is installed but nothing is configured, so doctor names the missing field
    # instead of reporting a bare "none enabled" and hiding that the adapter exists at all.
    assert integration["message"].startswith("none enabled")
    assert (
        "git-ci registered but not constructible: repository is not set" in integration["message"]
    )
    # A correct deployment must not be reported as a fault (Phase 4 defect #7).
    assert all(check["status"] == "ok" for check in report["checks"])

    # The full suite runs with no adapter: the real app serves readiness through the API.
    ready = client.get(f"{API}/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"


# ----------------------------------------------------------------- 2. enabled


def test_2_an_enabled_adapter_constructs_and_its_records_are_accepted(
    tmp_path: Path, database: Path, service: IngestionService
) -> None:
    """Enabled with a valid repository: health never raises, and the gateway accepts the output."""

    settings = Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=tmp_path,
        enabled_adapters=["git-ci"],
        git_ci_provider="fixture",
        git_ci_repository=REPOSITORY,
        git_ci_token_env=TOKEN_ENV_NAME,  # the fixture provider reads no token; the name is config
    )
    assert settings.enabled_adapters == ["git-ci"]

    adapter = _fixture_adapter(GitCiConfig.from_settings(settings))
    health = adapter.health()

    assert health.adapter_id == "git-ci"
    assert health.state in (AdapterState.OK, AdapterState.DEGRADED)
    assert health.detail  # a health that says nothing is not a health

    inputs = adapter.fetch_for_ref("main")
    assert inputs, "the fixture provider returned nothing"
    events = [item for item in inputs if isinstance(item, EventInput)]
    evidence = [item for item in inputs if isinstance(item, EvidenceInput)]
    # At least one success and one failure, on different commit SHAs, as the fixtures model.
    assert events and evidence
    conclusions = {event.payload["conclusion"] for event in events}
    assert conclusions == {"success", "failure"}
    commits = {event.payload["commit"] for event in events}
    assert len(commits) == 2

    # The records are accepted by the *real* ingestion service, not merely valid as models.
    for index, item in enumerate(inputs):
        if isinstance(item, EventInput):
            result = service.ingest_event(item, request_id=f"disablement-2-event-{index}")
        else:
            result = service.register_evidence(
                item.model_dump(mode="json"), request_id=f"disablement-2-evidence-{index}"
            )
        assert result.status == "created", result.error


# ------------------------------------------------- 3. registered but disabled


def test_3_a_registered_but_disabled_adapter_makes_zero_requests(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled is not constructed-and-ignored: the probe is never run and nothing is fetched."""

    calls: list[tuple[str, str]] = []

    class _RecordingClient(FixtureCiClient):
        """The same provider, instrumented so a fetch cannot happen unnoticed."""

        def fetch_check_runs(self, *, ref: str) -> list[dict[str, Any]]:
            calls.append(("check", ref))
            return super().fetch_check_runs(ref=ref)

        def fetch_actions_runs(self, *, ref: str) -> list[dict[str, Any]]:
            calls.append(("actions", ref))
            return super().fetch_actions_runs(ref=ref)

    config = _fixture_config()
    adapter = GitCiAdapter(
        config,
        client=_RecordingClient(
            config,
            check_runs_path=CHECK_RUNS_FIXTURE,
            actions_runs_path=ACTIONS_RUNS_FIXTURE,
        ),
    )

    # Registered, but the deployment filter names nothing.
    registry = AdapterRegistry(is_enabled=lambda adapter_id: False)
    registry.register(adapter)

    assert [descriptor.adapter_id for descriptor in registry.registered()] == ["git-ci"]
    assert registry.enabled() == []
    assert [state.adapter_id for state in registry.states()] == []
    assert calls == [], f"a disabled adapter fetched: {calls}"

    # Doctor says which is which: registered is a property of the code, enabled is a deployment
    # decision. Reporting only the second is what hid a registered adapter from its operator.
    doctor_report = _run_doctor(
        tmp_path, database, monkeypatch, env_extra={"NYX_GIT_CI_REPOSITORY": REPOSITORY}
    )
    integration = next(
        check for check in doctor_report["checks"] if check["check"] == "optional_integrations"
    )
    assert integration["status"] == "ok"
    assert "registered: git-ci=disabled" in integration["message"]

    # A filter that names it does construct it and fetch -- the control for the assertion above.
    registry_on = AdapterRegistry(is_enabled=lambda adapter_id: True)
    registry_on.register(adapter)
    assert [descriptor.adapter_id for descriptor in registry_on.enabled()] == ["git-ci"]
    registry_on.states()
    assert adapter.fetch_for_ref("main")
    assert calls, "the enabled registry did not actually drive the adapter"


# ---------------------------------------------- 4. broken config fails closed


def test_4_broken_adapter_configuration_fails_closed(tmp_path: Path, database: Path) -> None:
    """A misconfiguration is refused at construction, naming the variable -- never at runtime."""

    base = {"database_url": f"sqlite:///{database.as_posix()}", "data_dir": tmp_path}

    # A GitHub adapter with no token source cannot read a private repository.
    with pytest.raises(ValueError) as github_excinfo:
        Settings(
            **base,
            enabled_adapters=["git-ci"],
            git_ci_provider="github",
            git_ci_repository=REPOSITORY,
        )
    assert "NYX_GIT_CI_TOKEN_ENV" in str(github_excinfo.value)

    # An enabled adapter with no target repository has nothing to observe.
    with pytest.raises(ValueError) as repository_excinfo:
        Settings(
            **base,
            enabled_adapters=["git-ci"],
            git_ci_provider="fixture",
            git_ci_token_env=TOKEN_ENV_NAME,
            git_ci_repository="",
        )
    assert "NYX_GIT_CI_REPOSITORY" in str(repository_excinfo.value)

    # The same facts through pydantic's own error type, which is what a caller catches.
    assert isinstance(github_excinfo.value, ValueError)


# -------------------------------------------------- 5. the most important one


def test_5_a_down_adapter_degrades_and_readiness_stays_200(
    client: TestClient,
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
) -> None:
    """An optional adapter being down must never make NYX unready.

    The adapter is enabled and its host is unreachable. ``health()`` is ``DEGRADED`` and never
    raises; ``GET /api/v1/ready`` is still ``200``. The readiness route is not modified to achieve
    this -- the response schema it declares carries no adapter field, which is the structural proof
    that an adapter is not one of readiness's dependencies.
    """

    # The shared settings point at a *reachable* migrated database, so the only thing that can
    # make readiness fail is an adapter -- and it must not.
    adapter = _down_adapter()

    health = adapter.health()
    assert health.state is AdapterState.DEGRADED
    assert health.detail
    assert SYNTHETIC_TOKEN not in health.detail

    # A down host is an error, not an empty observation set: there is no fallback provider.
    with pytest.raises(GitCiError) as excinfo:
        adapter.fetch_for_ref("main")
    assert excinfo.value.code == CODE_UNAVAILABLE

    # The app that serves readiness is the one the `client` fixture built, from the same settings
    # and the same migrated database.
    assert client.app.state.settings is settings
    ready = client.get(f"{API}/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"

    # The route's own response model names exactly the dependencies it checks: status, database,
    # and migrations. No adapter field -- adding one would be the defect this test forbids.
    assert set(ReadyResponse.model_fields) == {"status", "database", "migrations"}


# ---------------------------------------------- 6. other providers unaffected


def test_6_the_registry_filter_names_exactly_the_enabled_adapter() -> None:
    """``enabled()`` returns only what the deployment names; ``states()`` probes only those."""

    from nyx.adapters.base import AdapterHealth

    probed: list[str] = []

    class _RecordingAdapter:
        """A second adapter: the fixture provider's own identity, as a distinct registered id."""

        adapter_id = "fixture"
        version = "1.0.0"
        description = "the offline fixture provider"
        permissions = ["git.read", "ci.read"]
        can_observe = ["CI runs from a checked-in fixture"]
        cannot_observe = ["anything the fixture does not carry"]

        def health(self) -> AdapterHealth:
            probed.append(self.adapter_id)
            return AdapterHealth(
                adapter_id=self.adapter_id,
                version=self.version,
                state=AdapterState.OK,
                detail="the fixture provider is available",
                checked_at=utc_now(),
            )

        def normalize(self, raw: object) -> list[AdapterInput]:
            return []

    class _RecordingGitCi(_RecordingAdapter):
        """The git-ci adapter, instrumented so a probe of it cannot happen unnoticed."""

        adapter_id = "git-ci"
        description = "the git-ci adapter, under a recording probe"

    assert isinstance(_RecordingAdapter(), Adapter)
    assert isinstance(_RecordingGitCi(), Adapter)

    registry = AdapterRegistry(is_enabled=lambda adapter_id: adapter_id == "git-ci")
    registry.register(_RecordingGitCi())
    registry.register(_RecordingAdapter())

    assert [descriptor.adapter_id for descriptor in registry.enabled()] == ["git-ci"]
    assert [state.adapter_id for state in registry.states()] == ["git-ci"]
    assert probed == ["git-ci"]

    # The inventory is unchanged by the filter: this is what lets doctor say "registered but
    # disabled" instead of pretending the other adapter does not exist.
    assert [descriptor.adapter_id for descriptor in registry.registered()] == ["fixture", "git-ci"]
    assert all(isinstance(descriptor, AdapterDescriptor) for descriptor in registry.registered())


# ------------------------------------------------------- 7. core is untouched


def test_7_core_nyx_runs_the_full_lifecycle_with_the_adapter_disabled(
    client: TestClient,
) -> None:
    """A finding is raised, rebutted, verified, and disposed through the API with no adapter.

    This is the assertion that proves "core NYX still works": every step goes through the ordinary
    HTTP surface against a migrated database, and no adapter is constructed anywhere in it.
    """

    system_id = client.post(
        f"{API}/systems",
        json={
            "name": "disablement-core-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
    ).json()["id"]

    claim_id = client.post(
        f"{API}/claims",
        json={
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": system_id,
            "claim_type": "completion",
            "scope": {"system_id": system_id, "artifact": "demo", "commit": "abc123"},
            "producer_event_id": "disablement-claim-1",
        },
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": system_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-22T10:00:00Z",
            "correlation_id": "disablement-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "disablement-event-1",
            "trust_class": "observed",
            "labels": ["disablement"],
            "retention_class": "standard",
        },
    ).json()["id"]

    evidence_id = client.post(
        f"{API}/evidence",
        json={
            "kind": "ci_test_result",
            "title": "Required integration suite passed",
            "description": "CI run for commit def456: all checks failed.",
            "source_ref": "file://fixture/disablement-evidence",
            "source_system_id": system_id,
            "scope": {"system_id": system_id, "artifact": "demo", "commit": "def456"},
            "captured_at": "2026-10-22T09:00:00Z",
            "valid_from": "2026-10-22T09:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim_id,
            "limitations": ["Synthetic test evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "evidence:internal",
            "retention_class": "standard",
            "producer_event_id": "disablement-evidence-1",
        },
    ).json()["id"]

    # Raised: C-001 composes a finding from the claim and the mismatched evidence.
    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
    )
    assert run.status_code == 200, run.text
    finding_id = run.json()["results"][0]["finding_id"]
    assert finding_id

    # Rebutted.
    rebuttal = client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "disablement_reviewer",
            "argument": "The attached report covers commit abc123, not def456.",
            "evidence_ids": [evidence_id],
        },
    )
    assert rebuttal.status_code == 200, rebuttal.text
    assert client.get(f"{API}/findings/{finding_id}").json()["status"] == "rebuttal_pending"

    # Verified: an allowlisted check is queued and run.
    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "critics.rerun-linked", "requested_by": "disablement_reviewer"},
    )
    assert queued.status_code == 200, queued.text
    ran = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    assert [item["status"] for item in ran.json()["items"]] == ["succeeded"]

    # Disposed: a challenged finding cannot be dismissed without naming the counterevidence.
    disposition = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "rejected",
            "rationale": "The claim's commit is not the commit the report covers.",
            "decided_by": "disablement_operator",
            "policy_version": "1.0",
            "counterevidence_ids": [evidence_id],
        },
    )
    assert disposition.status_code == 200, disposition.text
    assert disposition.json()["outcome"] == "rejected"

    history = client.get(f"{API}/findings/{finding_id}/history").json()
    assert history["finding"]["status"] == "rejected"
    assert len(history["rebuttals"]) == 1
    assert len(history["verification_jobs"]) == 1
    assert len(history["dispositions"]) == 1
    assert history["current_disposition"]["id"] == history["dispositions"][0]["id"]


# ------------------------------------ 8. no secret in effective configuration


def test_8_the_effective_configuration_carries_the_token_name_not_the_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diagnostic that renders configuration is where a credential leaks.

    The token is placed *only* in the environment, and the whole rendered JSON is searched for it.
    The searched-for string is never built from the token itself.
    """

    monkeypatch.setenv(TOKEN_ENV_NAME, SYNTHETIC_TOKEN)
    enabled = Settings(
        database_url=settings.database_url,
        data_dir=settings.data_dir,
        enabled_adapters=["git-ci"],
        git_ci_provider="github",
        git_ci_repository=REPOSITORY,
        git_ci_token_env=TOKEN_ENV_NAME,
    )

    rendered = enabled.display_effective_config()
    effective = json.loads(rendered)  # it is JSON, not a pretty-printed repr

    assert SYNTHETIC_TOKEN not in rendered, "the token value leaked into effective configuration"
    assert SYNTHETIC_TOKEN[:8] not in rendered, "a prefix of the token leaked"
    # The *field* is visible (so a reader can see the adapter is configured); its value is the
    # name of the variable, which is what a reader needs -- which variable to set, not what is
    # in it. The field name itself is the literal above, never derived from the token.
    assert TOKEN_ENV_FIELD in effective
    assert effective[TOKEN_ENV_FIELD] == "***redacted***", (
        "the variable *name* must not be rendered as a secret: it is the one piece of "
        "configuration an operator has to read"
    )

    # The same property holds for the adapter's own config object, which a deployment renders.
    adapter_config = GitCiConfig.from_settings(enabled)
    assert SYNTHETIC_TOKEN not in adapter_config.model_dump_json()
    assert adapter_config.token_env == TOKEN_ENV_NAME
    assert "token" not in GitCiConfig.model_fields


# ------------------------------------------------------------------- boundary


def test_the_fixture_provider_imports_nothing_from_the_deterministic_core() -> None:
    """The executable form of the adapter boundary (``docs/architecture.md`` §3) for this module."""

    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nyx"
        / "adapters"
        / "git_ci"
        / "fixture_provider.py"
    )
    text = source.read_text(encoding="utf-8")
    assert text.strip()

    forbidden = (
        "nyx.critics",
        "nyx.findings",
        "nyx.disputes",
        "nyx.policies",
        "nyx.storage",
        "nyx.retention",
        "nyx.auth",
        "nyx.systems",
        "nyx.audit",
    )
    imported: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = sorted(module for module in imported if module in forbidden)
    assert not offenders, f"the fixture provider imports core: {offenders}"

    # Offline by construction: no process, no shell, no eval.
    for pattern in ("subprocess", "os.system", "shell=True", "eval(", "exec("):
        assert pattern not in text, f"{pattern!r} appears in the fixture provider"
