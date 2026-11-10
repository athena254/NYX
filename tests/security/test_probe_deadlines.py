"""Windows process isolation for trusted probe callbacks.

These regressions use real sleeping callbacks rather than mocked clocks.  A timeout that merely
returns before an in-process thread is done is not a timeout: the callback can still alter state
after NYX has reported an outcome.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cloudpickle", reason="the reviewed trusted-extension serializer is required")

from nyx.domain.enums import Environment, ProbeKind, ProbeOutcome
from nyx.evidence import isolated
from nyx.evidence.probes import ProbeContext, make_observation
from nyx.evidence.registry import (
    Budget,
    ProbeRegistry,
    ProbeRegistryError,
)


def _context(**extra: object) -> ProbeContext:
    return ProbeContext(
        subject="deadline-test",
        environment=Environment.LOCAL,
        extra=extra,
    )


class _CallbackProbe:
    probe_id = "probe:deadline-callback"
    probe_version = "1.0.0"
    kind = ProbeKind.STATE_CHECK
    read_only = True
    description = "Trusted test callback executed by the isolated probe worker."

    def __init__(self, callback: Any) -> None:
        self._callback = callback

    def run(self, target: str, context: ProbeContext):  # type: ignore[no-untyped-def]
        payload = self._callback(target, context)
        return make_observation(
            probe=self,
            target=target,
            context=context,
            outcome=ProbeOutcome.OBSERVED,
            detail="trusted callback completed",
            payload=payload,
        )


def _is_process_alive(pid: int) -> bool:
    if os.name != "nt":
        pytest.skip("Windows Job Object containment is Windows-specific")

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _write_marker_after_delay(marker: str, delay_seconds: float) -> None:
    time.sleep(delay_seconds)
    Path(marker).write_text("late", encoding="utf-8")


def test_timeout_returns_error_and_kills_the_callback_before_it_can_mutate(tmp_path: Path) -> None:
    """The primary regression: no successful result or late side effect after deadline expiry.

    The budget is deliberately generous relative to the callback's sleep. Startup of the spawned
    worker is part of the budget (the plan's rule), and on a loaded machine spawn alone can take
    longer than a small budget; a budget that the worker cannot reach makes this test assert a
    startup race instead of the timeout property it is about. The callback sleeps well past the
    budget, so the timeout still fires, and ``elapsed`` is still bounded above.
    """

    marker = tmp_path / "late-marker.txt"
    worker_pid = tmp_path / "worker-pid.txt"

    def slow_callback(_: str, __: ProbeContext) -> dict[str, str]:
        worker_pid.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(20.0)
        marker.write_text("late", encoding="utf-8")
        return {"completed": "too late"}

    registry = ProbeRegistry()
    registry.register(_CallbackProbe(slow_callback))

    started = time.monotonic()
    observation = registry.run_probe(
        "probe:deadline-callback",
        "deadline-target",
        _context(),
        budget=Budget(timeout_seconds=8.0),
    )
    elapsed = time.monotonic() - started

    assert observation.result is ProbeOutcome.ERROR
    assert elapsed < 10.0
    assert worker_pid.exists(), "the test must observe the owned worker identity"
    assert not _is_process_alive(int(worker_pid.read_text(encoding="utf-8")))
    time.sleep(0.75)
    assert not marker.exists(), "a timed-out callback must not mutate state after NYX returned"


def test_a_serializable_closure_still_completes_quickly() -> None:
    expected = "closure value"

    def quick_callback(_: str, __: ProbeContext) -> dict[str, str]:
        return {"value": expected}

    registry = ProbeRegistry()
    registry.register(_CallbackProbe(quick_callback))
    observation = registry.run_probe(
        "probe:deadline-callback",
        "quick-target",
        _context(),
        budget=Budget(timeout_seconds=8.0),
    )

    assert observation.result is ProbeOutcome.OBSERVED
    assert observation.payload == {"value": expected}


def test_callback_exception_and_worker_crash_become_error_observations() -> None:
    def raises(_: str, __: ProbeContext) -> None:
        raise RuntimeError("trusted callback failed")

    def crashes(_: str, __: ProbeContext) -> None:
        os._exit(23)

    registry = ProbeRegistry()
    registry.register(_CallbackProbe(raises))
    exception = registry.run_probe(
        "probe:deadline-callback",
        "exception-target",
        _context(),
        budget=Budget(timeout_seconds=8.0),
    )
    registry.register(_CallbackProbe(crashes))
    crashed = registry.run_probe(
        "probe:deadline-callback",
        "crash-target",
        _context(),
        budget=Budget(timeout_seconds=8.0),
    )

    assert exception.result is ProbeOutcome.ERROR
    assert crashed.result is ProbeOutcome.ERROR


def test_oversized_input_and_output_are_refused_or_reported_without_running_unbounded() -> None:
    registry = ProbeRegistry()
    registry.register(_CallbackProbe(lambda _target, _context: {"ok": "small"}))

    with pytest.raises(ProbeRegistryError, match="payload"):
        registry.run_probe(
            "probe:deadline-callback",
            "large-input",
            _context(blob="x" * 100_000),
            budget=Budget(timeout_seconds=8.0),
        )

    registry.register(_CallbackProbe(lambda _target, _context: {"large": "x" * 100_000}))
    output = registry.run_probe(
        "probe:deadline-callback",
        "large-output",
        _context(),
        budget=Budget(timeout_seconds=8.0),
    )
    assert output.result is ProbeOutcome.ERROR


def test_batch_uses_one_deadline_not_a_full_timeout_per_callback() -> None:
    def slow_callback(target: str, __: ProbeContext) -> dict[str, str]:
        time.sleep(0.5 if target == "first" else 12.0)
        return {"finished": "after shared deadline"}

    registry = ProbeRegistry()
    registry.register(_CallbackProbe(slow_callback))

    observations = registry.run_many(
        [
            ("probe:deadline-callback", "first", _context()),
            ("probe:deadline-callback", "second", _context()),
        ],
        budget=Budget(max_steps=2, timeout_seconds=8.0),
    )

    assert [observation.result for observation in observations] == [
        ProbeOutcome.OBSERVED,
        ProbeOutcome.ERROR,
    ]


def test_timeout_closes_the_job_and_kills_descendants(tmp_path: Path) -> None:
    """A callback child must be in NYX's job before callback execution is released."""

    descendant_pid = tmp_path / "descendant-pid.txt"
    marker = tmp_path / "descendant-marker.txt"

    def spawn_descendant(_: str, __: ProbeContext) -> dict[str, str]:
        child = multiprocessing.get_context("spawn").Process(
            target=_write_marker_after_delay,
            args=(str(marker), 40.0),
        )
        child.start()
        descendant_pid.write_text(str(child.pid), encoding="utf-8")
        time.sleep(30.0)
        return {"completed": "too late"}

    registry = ProbeRegistry()
    registry.register(_CallbackProbe(spawn_descendant))
    observation = registry.run_probe(
        "probe:deadline-callback",
        "descendant-target",
        _context(),
        budget=Budget(timeout_seconds=15.0),
    )

    assert observation.result is ProbeOutcome.ERROR
    assert descendant_pid.exists(), "the callback must have created a descendant before timeout"
    assert not _is_process_alive(int(descendant_pid.read_text(encoding="utf-8")))
    time.sleep(0.75)
    assert not marker.exists(), "job closure must kill descendants as well as the wrapper process"


def test_registry_import_does_not_require_the_windows_only_pipe_annotation() -> None:
    """Controlled absence proves the registry import does not need ``PipeConnection`` at runtime."""

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and inline controlled source
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """
                import multiprocessing.connection as connection
                if hasattr(connection, "PipeConnection"):
                    del connection.PipeConnection
                import nyx.evidence.registry
                """
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_real_cloudpickle_runtime_reducer_is_refused_before_its_reducer_runs(
    tmp_path: Path,
) -> None:
    """The outer timeout bounds this discriminating real-cloudpickle regression itself.

    This verifies the runtime data boundary, not registration preparation: preparation may run
    trusted serializers before any request and is deliberately outside the execution deadline.
    """

    script = tmp_path / "slow_runtime_reducer.py"
    script.write_text(
        textwrap.dedent(
            """
            import time
            import cloudpickle

            from nyx.domain.enums import Environment, ProbeKind, ProbeOutcome
            from nyx.evidence.probes import ProbeContext, make_observation
            from nyx.evidence.registry import Budget, ProbeExecutionRefused, ProbeRegistry

            assert cloudpickle.__version__ == "3.1.2"

            class SlowReducer:
                def __reduce__(self):
                    time.sleep(0.4)
                    return (str, ("slow",))

            class QuickProbe:
                probe_id = "probe:slow-runtime-reducer"
                probe_version = "1.0.0"
                kind = ProbeKind.STATE_CHECK
                read_only = True
                description = "Quick probe with hostile runtime data."

                def run(self, target, context):
                    return make_observation(
                        probe=self,
                        target=target,
                        context=context,
                        outcome=ProbeOutcome.OBSERVED,
                        detail="quick",
                    )

            registry = ProbeRegistry()
            registry.register(QuickProbe())
            started = time.monotonic()
            try:
                registry.run_probe(
                    "probe:slow-runtime-reducer",
                    "target",
                    ProbeContext(
                        subject="subject",
                        environment=Environment.LOCAL,
                        extra={"slow": SlowReducer()},
                    ),
                    budget=Budget(timeout_seconds=0.1),
                )
            except ProbeExecutionRefused:
                elapsed = time.monotonic() - started
                assert elapsed < 0.1, elapsed
            else:
                raise AssertionError("runtime extension data was not refused")
            """
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(Path.cwd() / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned harness
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_real_cloudpickle_slow_reconstruction_is_not_decoded_in_the_parent(tmp_path: Path) -> None:
    """The real serializer creates the hostile bytes; the parent must treat them as invalid JSON."""

    script = tmp_path / "slow_result_reconstruction.py"
    script.write_text(
        textwrap.dedent(
            """
            import time
            import cloudpickle

            from nyx.domain.base import PrincipalRef, utc_now
            from nyx.domain.enums import (
                Environment, PrincipalKind, ProbeKind, ProbeOutcome, VerificationStrength,
            )
            from nyx.domain.observation import Observation
            from nyx.evidence import isolated

            assert cloudpickle.__version__ == "3.1.2"

            def rebuild():
                time.sleep(0.4)
                return Observation(
                    probe_id="probe:slow-result",
                    probe_version="1.0.0",
                    subject="subject",
                    collected_at=utc_now(),
                    collector=PrincipalRef(id="nyx:test", kind=PrincipalKind.SYSTEM),
                    method=ProbeKind.STATE_CHECK,
                    result=ProbeOutcome.OBSERVED,
                    environment=Environment.LOCAL,
                    verification_strength=VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
                    detail="late",
                )

            class SlowObservation:
                def __reduce__(self):
                    return (rebuild, ())

            message = b"O" + cloudpickle.dumps(SlowObservation())
            started = time.monotonic()
            result = isolated._decode_worker_message(cloudpickle, message, 11)
            elapsed = time.monotonic() - started
            assert result.status == "invalid_output", result
            assert elapsed < 0.1, elapsed
            """
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(Path.cwd() / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned harness
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
        env=environment,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_start_error_is_not_masked_by_unstarted_process_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed process start has no process handle that may be joined or closed."""

    class Connection:
        def close(self) -> None:
            pass

        def send_bytes(self, _: bytes) -> None:
            raise AssertionError("payload must not be released after process.start() failed")

    class Process:
        pid = None

        def start(self) -> None:
            raise OSError("process creation failed")

        def join(self, timeout: float) -> None:
            raise AssertionError("unstarted process must not be joined")

    class Context:
        def Pipe(self, *, duplex: bool) -> tuple[Connection, Connection]:
            assert duplex is True
            return Connection(), Connection()

        def Process(self, **_: object) -> Process:
            return Process()

    class Job:
        @classmethod
        def create_kill_on_close(cls) -> Job:
            return cls()

        def close(self) -> None:
            pass

    context = Context()
    monkeypatch.setattr(isolated.multiprocessing, "get_context", lambda _: context)
    monkeypatch.setattr(isolated, "_WindowsJob", Job)

    result = isolated.execute_isolated_probe(
        isolated.prepare_trusted_probe(_CallbackProbe(lambda _target, _context: {"never": "runs"})),
        "target",
        _context(),
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert result.status == "startup_error"
    assert result.error_type == "OSError"


def test_assignment_failure_never_releases_the_prepared_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows Job assignment remains the release gate for executable payload bytes."""

    class Connection:
        sent = False

        def close(self) -> None:
            pass

        def send_bytes(self, _: bytes) -> None:
            self.sent = True
            raise AssertionError("payload release must follow successful assignment")

    class Process:
        pid = 77
        exitcode = 0

        def start(self) -> None:
            pass

        def join(self, timeout: float) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def close(self) -> None:
            pass

    class Context:
        def Pipe(self, *, duplex: bool) -> tuple[Connection, Connection]:
            assert duplex is True
            return Connection(), Connection()

        def Process(self, **_: object) -> Process:
            return Process()

    class Job:
        @classmethod
        def create_kill_on_close(cls) -> Job:
            return cls()

        def assign(self, _: int) -> None:
            raise OSError("job assignment failed")

        def close(self) -> None:
            pass

    monkeypatch.setattr(isolated.multiprocessing, "get_context", lambda _: Context())
    monkeypatch.setattr(isolated, "_WindowsJob", Job)
    result = isolated.execute_isolated_probe(
        isolated.prepare_trusted_probe(_CallbackProbe(lambda _target, _context: {"never": "runs"})),
        "target",
        _context(),
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert result.status == "startup_error"
    assert result.error_type == "OSError"
