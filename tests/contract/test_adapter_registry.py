"""Contract tests for the adapter registry and the adapter boundary (ADR 0008).

The registry is the code inventory and ``Settings.enabled_adapters`` is the deployment
filter; these tests pin the difference, because Phase 4 defect #7 was a doctor check that
reported *configuration* as *availability*. A registry that cannot say "registered but
disabled" honestly would let that defect return.

The boundary test is the executable form of ``docs/architecture.md`` §3: an adapter is a
producer behind the ingestion gateway, and core never imports it.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from nyx.adapters.base import (
    Adapter,
    AdapterDescriptor,
    AdapterHealth,
    AdapterInput,
    AdapterRegistry,
    AdapterState,
    UnknownAdapterError,
)
from nyx.domain.base import utc_now

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPOSITORY_ROOT / "src"

#: The deterministic core. A module under ``src/nyx/adapters/`` may import ``nyx.domain``
#: and ``nyx.ingestion`` and nothing from this list.
_FORBIDDEN_CORE_MODULES: tuple[str, ...] = (
    "nyx.audit",
    "nyx.auth",
    "nyx.critics",
    "nyx.disputes",
    "nyx.findings",
    "nyx.policies",
    "nyx.retention",
    "nyx.storage",
    "nyx.systems",
)

#: The only files outside the adapter package allowed to import it. They construct
#: adapters; nothing else in NYX may.
_ORCHESTRATOR_IMPORTERS = {"api/app.py", "cli/main.py"}


class _FakeAdapter:
    """A minimal structural implementation of the ``Adapter`` protocol."""

    def __init__(
        self,
        adapter_id: str,
        *,
        health_error: Exception | None = None,
        state: AdapterState = AdapterState.OK,
        detail: str = "fake adapter is fine",
    ) -> None:
        self.adapter_id = adapter_id
        self.version = "1.0.0"
        self.description = f"{adapter_id} test adapter"
        self.permissions = ["events.write"]
        self.can_observe = ["CI runs"]
        self.cannot_observe = ["the truth of a claim"]
        self._health_error = health_error
        self._state = state
        self._detail = detail

    def health(self) -> AdapterHealth:
        if self._health_error is not None:
            raise self._health_error
        return AdapterHealth(
            adapter_id=self.adapter_id,
            version=self.version,
            state=self._state,
            detail=self._detail,
            checked_at=utc_now(),
        )

    def normalize(self, raw: object) -> list[AdapterInput]:
        return []


class _NotAnAdapter:
    """Satisfies the identity attributes but not the protocol: no ``normalize``."""

    adapter_id = "not-an-adapter"
    version = "1.0.0"
    description = "missing normalize"


# ---------------------------------------------------------------------------
# Registration and lookup
# ---------------------------------------------------------------------------


def test_register_replaces_on_the_same_id() -> None:
    registry = AdapterRegistry()
    registry.register(_FakeAdapter("git-ci"))
    registry.register(_FakeAdapter("git-ci", state=AdapterState.DEGRADED))

    assert [descriptor.adapter_id for descriptor in registry.registered()] == ["git-ci"]
    assert registry.get("git-ci")._state is AdapterState.DEGRADED  # type: ignore[attr-defined]


def test_get_unknown_adapter_raises() -> None:
    registry = AdapterRegistry()

    with pytest.raises(UnknownAdapterError, match="registered adapters"):
        registry.get("git-ci")


# ---------------------------------------------------------------------------
# The inventory / deployment-filter distinction
# ---------------------------------------------------------------------------


def test_registered_returns_the_code_inventory_in_stable_order() -> None:
    registry = AdapterRegistry()
    registry.register(_FakeAdapter("zeta"))
    registry.register(_FakeAdapter("git-ci"))

    descriptors = registry.registered()

    assert [descriptor.adapter_id for descriptor in descriptors] == ["git-ci", "zeta"]
    first = descriptors[0]
    assert isinstance(first, AdapterDescriptor)
    assert first.permissions == ["events.write"]
    assert first.can_observe == ["CI runs"]
    assert first.cannot_observe == ["the truth of a claim"]


def test_enabled_applies_the_deployment_filter() -> None:
    registry = AdapterRegistry(is_enabled=lambda adapter_id: adapter_id == "git-ci")
    registry.register(_FakeAdapter("git-ci"))
    registry.register(_FakeAdapter("zeta"))

    assert [descriptor.adapter_id for descriptor in registry.enabled()] == ["git-ci"]
    # The inventory is unchanged by the filter: this is what lets doctor report
    # "registered: zeta; enabled: false" instead of pretending zeta does not exist.
    assert [descriptor.adapter_id for descriptor in registry.registered()] == ["git-ci", "zeta"]


# ---------------------------------------------------------------------------
# Health: a broken probe is reported, never raised
# ---------------------------------------------------------------------------


def test_a_raising_health_probe_is_reported_as_degraded_and_never_raises() -> None:
    # A synthetic credential, used only as input: the assertion is that it never appears in
    # the registry's output. Building the searched-for string from the secret itself would
    # report a leak the test caused (HANDOFF.md §7 records that exact mistake).
    secret_url = "https://user:hunter2@example.invalid/api"  # noqa: S105 - test fixture, not a credential
    registry = AdapterRegistry()
    registry.register(_FakeAdapter("git-ci", health_error=RuntimeError(f"boom at {secret_url}")))

    states = registry.states()

    assert len(states) == 1
    state = states[0]
    assert state.state is AdapterState.DEGRADED
    assert state.adapter_id == "git-ci"
    # The exception *type* is reported; the message is not, because it can echo a URL.
    assert "RuntimeError" in state.detail
    assert "boom" not in state.detail
    assert secret_url not in state.detail


def test_states_probes_only_enabled_adapters() -> None:
    """A disabled adapter is not constructed, so there is nothing to probe."""

    probed: list[str] = []

    class _Recording(_FakeAdapter):
        def health(self) -> AdapterHealth:
            probed.append(self.adapter_id)
            return super().health()

    registry = AdapterRegistry(is_enabled=lambda adapter_id: adapter_id == "git-ci")
    registry.register(_Recording("git-ci"))
    registry.register(_Recording("zeta"))

    states = registry.states()

    assert [state.adapter_id for state in states] == ["git-ci"]
    assert probed == ["git-ci"]


def test_states_reports_an_ok_adapter() -> None:
    registry = AdapterRegistry()
    registry.register(_FakeAdapter("git-ci", state=AdapterState.OK))

    state = registry.states()[0]

    assert state.state is AdapterState.OK
    # The probe stamped a real UTC time, not a defaulted or naive one.
    assert state.checked_at.tzinfo is not None
    assert abs((utc_now() - state.checked_at).total_seconds()) < 5


# ---------------------------------------------------------------------------
# The Protocol is structural
# ---------------------------------------------------------------------------


def test_a_class_satisfying_the_protocol_is_recognised() -> None:
    assert isinstance(_FakeAdapter("git-ci"), Adapter)


def test_a_class_missing_normalize_is_not_an_adapter() -> None:
    assert not isinstance(_NotAnAdapter(), Adapter)


# ---------------------------------------------------------------------------
# Gate G7 — the boundary, as an executable assertion
# ---------------------------------------------------------------------------


def _containing_package(path: Path) -> str:
    return ".".join(path.relative_to(_SRC_ROOT).parts[:-1])


def _absolute_module(module: str | None, level: int, package: str) -> str | None:
    if level == 0 or module is None:
        return module
    parts = package.split(".")
    base = parts if level == 1 else parts[: len(parts) - (level - 1)]
    if not base:
        return module
    return ".".join([*base, module])


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _containing_package(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            absolute = _absolute_module(node.module, node.level, package)
            if absolute:
                found.add(absolute)
    return found


def test_no_adapter_module_imports_the_deterministic_core() -> None:
    adapters_root = _SRC_ROOT / "nyx" / "adapters"
    assert adapters_root.is_dir(), "the adapter package root is missing"

    offenders: list[str] = []
    for path in sorted(adapters_root.rglob("*.py")):
        for module in _imported_modules(path):
            if module in _FORBIDDEN_CORE_MODULES or module.startswith(
                tuple(f"{name}." for name in _FORBIDDEN_CORE_MODULES)
            ):
                offenders.append(f"{path.relative_to(_SRC_ROOT).as_posix()} imports {module}")

    assert not offenders, "the adapter boundary is broken:\n" + "\n".join(offenders)


def test_only_the_orchestrator_files_import_the_adapter_package() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(_SRC_ROOT / "nyx").as_posix()
        if relative.startswith("adapters/"):
            continue
        imported = _imported_modules(path)
        imports_adapters = "nyx.adapters" in imported or any(
            module.startswith("nyx.adapters.") for module in imported
        )
        if imports_adapters and relative not in _ORCHESTRATOR_IMPORTERS:
            offenders.append(f"{relative} imports nyx.adapters")

    assert not offenders, "core must not import adapters:\n" + "\n".join(offenders)


def test_the_orchestrator_files_are_exactly_the_two_expected() -> None:
    """Pin the allowlist itself, so adding a third importer is a deliberate edit."""

    assert {"api/app.py", "cli/main.py"} == _ORCHESTRATOR_IMPORTERS
    assert (_SRC_ROOT / "nyx" / "api" / "app.py").is_file()
    assert (_SRC_ROOT / "nyx" / "cli" / "main.py").is_file()


def test_a_naive_datetime_is_rejected_by_the_health_model() -> None:
    """Health carries a UTC timestamp; a naive one is a silent local-time bug."""

    with pytest.raises(ValidationError, match="aware"):
        AdapterHealth(
            adapter_id="git-ci",
            version="1.0.0",
            state=AdapterState.OK,
            detail="fine",
            checked_at=datetime(2026, 10, 21, 12, 0),  # noqa: DTZ001 - deliberately naive
        )
