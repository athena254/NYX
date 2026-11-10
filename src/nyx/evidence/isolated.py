"""Cancellable containment for trusted, pre-registered probe extensions.

Preparation serializes trusted code during registry configuration, outside a later request budget.
Execution accepts only bounded exact JSON data and returns JSON for the fixed Observation model.
Trusted code is contained, not sandboxed; API callers never supply pickle or executable code.
"""

from __future__ import annotations

import ctypes
import json
import math
import multiprocessing
import os
import signal
import time
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import ValidationError

from nyx.domain.base import PrincipalRef
from nyx.domain.enums import Environment
from nyx.domain.observation import Observation
from nyx.evidence.probes import ProbeContext

if TYPE_CHECKING:
    from nyx.evidence.probes import Probe, Target

MAX_INPUT_BYTES = 12 * 1024
MAX_RESULT_BYTES = 12 * 1024
_MAX_WIRE_BYTES = MAX_RESULT_BYTES + 256
_MAX_JSON_DEPTH = 16
_MAX_JSON_ITEMS = 256


class IsolatedExecutionRefused(Exception):
    """The trusted callback or runtime data cannot safely enter process isolation."""


class IsolatedPayloadRefused(IsolatedExecutionRefused):
    """A trusted payload exceeds a reviewed fixed boundary."""


@dataclass(frozen=True)
class IsolatedExecutionResult:
    status: Literal[
        "completed",
        "timeout",
        "worker_crashed",
        "callback_error",
        "output_too_large",
        "invalid_output",
        "startup_error",
    ]
    worker_pid: int | None
    value: Observation | None = None
    error_type: str | None = None


def prepare_trusted_probe(probe: Probe) -> bytes:
    """Prepare trusted executable configuration; this is intentionally not request-budgeted."""

    try:
        payload = cast(bytes, _cloudpickle().dumps(probe))
    except Exception as exc:  # noqa: BLE001 - unsupported trusted configuration is refused
        raise IsolatedExecutionRefused(
            "trusted probe callback is not serializable during registration preparation: "
            f"{type(exc).__name__}"
        ) from None
    if len(payload) > MAX_INPUT_BYTES:
        raise IsolatedPayloadRefused(
            f"prepared trusted probe is {len(payload)} bytes, above the "
            f"{MAX_INPUT_BYTES}-byte limit"
        )
    return payload


def execute_isolated_probe(
    prepared_probe: bytes,
    target: Target,
    context: ProbeContext,
    *,
    deadline_monotonic: float,
) -> IsolatedExecutionResult:
    """Run prepared code with one shared deadline and a finite cleanup allowance."""

    runtime_payload = _encode_runtime_input(target, context)
    if _remaining(deadline_monotonic) <= 0:
        return IsolatedExecutionResult(status="timeout", worker_pid=None)

    parent: Any = None
    child: Any = None
    process: SpawnProcess | None = None
    job: _WindowsJob | None = None
    started = False
    posix_group_owned = False
    worker_pid: int | None = None
    try:
        parent, child = multiprocessing.get_context("spawn").Pipe(duplex=True)
        if os.name == "nt":
            job = _WindowsJob.create_kill_on_close()
        # Ensure the spawned worker can import the project package (src layout) even though
        # pytest's pythonpath setting is not inherited by spawn-based child processes.
        src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        project_root = os.path.dirname(src_dir)
        import sys as _sys

        for d in (src_dir, project_root):
            if d not in _sys.path:
                _sys.path.insert(0, d)
        existing = os.environ.get("PYTHONPATH", "")
        parts = [p for p in existing.split(os.pathsep) if p]
        for d in (src_dir, project_root):
            if d not in parts:
                parts.insert(0, d)
        os.environ["PYTHONPATH"] = os.pathsep.join(parts)
        process = multiprocessing.get_context("spawn").Process(
            target=_isolated_worker, args=(child,), name="nyx-trusted-probe"
        )
        process.start()
        started = True
        child.close()
        child = None
        worker_pid = process.pid
        if worker_pid is None:
            return IsolatedExecutionResult(status="startup_error", worker_pid=None)
        if _remaining(deadline_monotonic) <= 0:
            return IsolatedExecutionResult(status="timeout", worker_pid=worker_pid)

        # On Windows, callback code remains unread until Job Object assignment succeeds.
        if job is not None:
            job.assign(worker_pid)
        if os.name == "posix":
            if not _await_ready(parent, process, deadline_monotonic):
                return IsolatedExecutionResult(status="startup_error", worker_pid=worker_pid)
            if not _owns_posix_group(worker_pid):
                return IsolatedExecutionResult(status="startup_error", worker_pid=worker_pid)
            posix_group_owned = True
        if _remaining(deadline_monotonic) <= 0:
            return IsolatedExecutionResult(status="timeout", worker_pid=worker_pid)

        # The child receives executable bytes only after this platform's containment admission.
        parent.send_bytes(prepared_probe)
        parent.send_bytes(runtime_payload)
        return _receive_worker_result(parent, process, worker_pid, deadline_monotonic)
    except OSError as exc:
        return IsolatedExecutionResult(
            status="startup_error", worker_pid=worker_pid, error_type=type(exc).__name__
        )
    finally:
        if parent is not None:
            parent.close()
        if child is not None:
            child.close()
        if job is not None:
            job.close()
        if process is not None and started:
            _reap_process(process, posix_group_owned=posix_group_owned)


def _cloudpickle() -> Any:
    try:
        import cloudpickle  # type: ignore[import-untyped]
    except ImportError as exc:
        raise IsolatedExecutionRefused(
            "trusted probe isolation requires reviewed cloudpickle; "
            "refusing an uncancellable fallback"
        ) from exc
    return cloudpickle


def _encode_runtime_input(target: Target, context: ProbeContext) -> bytes:
    """Reject hooks/subclasses before encoding any runtime request data in the parent."""

    if type(target) is not str:
        raise IsolatedExecutionRefused("probe target must be an exact str for isolated execution")
    if type(context) is not ProbeContext:
        raise IsolatedExecutionRefused(
            "probe context must be an exact ProbeContext for isolated execution"
        )
    if type(context.subject) is not str:
        raise IsolatedExecutionRefused("probe context subject must be an exact str")
    if context.subject_version is not None and type(context.subject_version) is not str:
        raise IsolatedExecutionRefused("probe context subject_version must be str or None")
    if type(context.environment) is not Environment:
        raise IsolatedExecutionRefused("probe context environment must be an Environment value")
    if type(context.timeout_seconds) not in {int, float} or isinstance(
        context.timeout_seconds, bool
    ):
        raise IsolatedExecutionRefused("probe context timeout_seconds must be a finite number")
    if not math.isfinite(float(context.timeout_seconds)):
        raise IsolatedExecutionRefused("probe context timeout_seconds must be finite")
    if context.collector is None:
        collector: dict[str, Any] | None = None
    elif type(context.collector) is PrincipalRef:
        collector = context.collector.model_dump(mode="json")
    else:
        raise IsolatedExecutionRefused("probe context collector must be a PrincipalRef or None")
    raw = {
        "target": target,
        "subject": context.subject,
        "subject_version": context.subject_version,
        "environment": context.environment.value,
        "collector": collector,
        "claim_id": _exact_json(context.claim_id, field="claim_id"),
        "timeout_seconds": context.timeout_seconds,
        "extra": _exact_json(context.extra, field="extra"),
    }
    encoded = json.dumps(raw, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise IsolatedPayloadRefused(
            f"probe runtime payload is {len(encoded)} bytes, above the {MAX_INPUT_BYTES}-byte limit"
        )
    return encoded


def _exact_json(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise IsolatedExecutionRefused(f"probe runtime {field} exceeds JSON nesting limit")
    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise IsolatedExecutionRefused(f"probe runtime {field} contains a non-finite float")
        return value
    if type(value) is list:
        if len(value) > _MAX_JSON_ITEMS:
            raise IsolatedExecutionRefused(f"probe runtime {field} exceeds JSON item limit")
        return [_exact_json(item, field=field, depth=depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_JSON_ITEMS:
            raise IsolatedExecutionRefused(f"probe runtime {field} exceeds JSON item limit")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise IsolatedExecutionRefused(f"probe runtime {field} has a non-str key")
            result[key] = _exact_json(item, field=field, depth=depth + 1)
        return result
    raise IsolatedExecutionRefused(
        f"probe runtime {field} contains unsupported type {type(value).__name__}; "
        "only exact JSON primitives, lists, and dicts are supported"
    )


def _isolated_worker(connection: Connection) -> None:
    """Set POSIX containment before receiving code and return fixed JSON only."""

    try:
        if os.name == "posix":
            os.setsid()  # type: ignore[attr-defined]
            connection.send_bytes(b"R")
        prepared_probe = connection.recv_bytes(MAX_INPUT_BYTES)
        runtime_payload = connection.recv_bytes(MAX_INPUT_BYTES)
        probe = _cloudpickle().loads(prepared_probe)
        target, context = _decode_runtime_input(runtime_payload)
        observation = probe.run(target, context)
        if type(observation) is not Observation:
            _send_worker_status(connection, b"I")
            return
        encoded = json.dumps(
            observation.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        _send_worker_status(connection, b"P" if len(encoded) > MAX_RESULT_BYTES else b"O" + encoded)
    except Exception as exc:  # noqa: BLE001 - callback errors become operational results
        _send_worker_status(connection, b"E" + type(exc).__name__.encode("ascii", "replace")[:128])
    finally:
        connection.close()


def _decode_runtime_input(payload: bytes) -> tuple[str, ProbeContext]:
    raw = json.loads(payload)
    if type(raw) is not dict or type(raw.get("target")) is not str:
        raise ValueError("runtime input is not a valid object")
    collector_data = raw["collector"]
    collector = None if collector_data is None else PrincipalRef.model_validate(collector_data)
    return raw["target"], ProbeContext(
        subject=raw["subject"],
        subject_version=raw["subject_version"],
        environment=Environment(raw["environment"]),
        collector=collector,
        claim_id=raw["claim_id"],
        timeout_seconds=raw["timeout_seconds"],
        extra=raw["extra"],
    )


def _send_worker_status(connection: Connection, message: bytes) -> None:
    with suppress(OSError):
        connection.send_bytes(message)


def _await_ready(connection: Connection, process: SpawnProcess, deadline: float) -> bool:
    while _remaining(deadline) > 0:
        if connection.poll(min(_remaining(deadline), 0.05)):
            try:
                return connection.recv_bytes(16) == b"R" and _remaining(deadline) > 0
            except (EOFError, OSError):
                return False
        if not process.is_alive():
            return False
    return False


def _receive_worker_result(
    connection: Connection, process: SpawnProcess, worker_pid: int, deadline: float
) -> IsolatedExecutionResult:
    while True:
        remaining = _remaining(deadline)
        if remaining <= 0:
            return IsolatedExecutionResult(status="timeout", worker_pid=worker_pid)
        if connection.poll(min(remaining, 0.05)):
            try:
                message = connection.recv_bytes(_MAX_WIRE_BYTES)
            except (EOFError, OSError) as exc:
                return IsolatedExecutionResult(
                    status="worker_crashed", worker_pid=worker_pid, error_type=type(exc).__name__
                )
            if _remaining(deadline) <= 0:
                return IsolatedExecutionResult(status="timeout", worker_pid=worker_pid)
            result = _decode_worker_message(None, message, worker_pid)
            return (
                result
                if _remaining(deadline) > 0
                else IsolatedExecutionResult(status="timeout", worker_pid=worker_pid)
            )
        if not process.is_alive():
            return IsolatedExecutionResult(
                status="worker_crashed",
                worker_pid=worker_pid,
                error_type=f"exit_{process.exitcode}",
            )


def _decode_worker_message(_: Any, message: bytes, worker_pid: int) -> IsolatedExecutionResult:
    """The parent decodes only fixed JSON; it never invokes cloudpickle.loads on results."""

    if message == b"P":
        return IsolatedExecutionResult(status="output_too_large", worker_pid=worker_pid)
    if message.startswith(b"E"):
        return IsolatedExecutionResult(
            status="callback_error",
            worker_pid=worker_pid,
            error_type=message[1:].decode("ascii", "replace") or "Exception",
        )
    if not message.startswith(b"O"):
        return IsolatedExecutionResult(status="invalid_output", worker_pid=worker_pid)
    try:
        raw = json.loads(message[1:])
        if type(raw) is not dict:
            raise ValueError("worker result is not an object")
        value = Observation.model_validate(raw)
    except (json.JSONDecodeError, RecursionError, TypeError, ValidationError, ValueError) as exc:
        return IsolatedExecutionResult(
            status="invalid_output", worker_pid=worker_pid, error_type=type(exc).__name__
        )
    return IsolatedExecutionResult(status="completed", worker_pid=worker_pid, value=value)


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _owns_posix_group(pid: int) -> bool:
    return bool(
        os.getpgid(pid) == pid  # type: ignore[attr-defined]
        and os.getpgrp() != pid  # type: ignore[attr-defined]
    )


def _reap_process(process: SpawnProcess, *, posix_group_owned: bool) -> None:
    if posix_group_owned and process.pid is not None:
        with suppress(ProcessLookupError, OSError):
            if _owns_posix_group(process.pid):
                os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    process.join(timeout=1.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)
    if not process.is_alive():
        process.close()


class _LargeInteger(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LargeInteger),
        ("PerJobUserTimeLimit", _LargeInteger),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win_error() -> Any:
    """Return the Windows ``WinError`` factory, or raise a portable error off Windows.

    ``ctypes.WinError`` exists only on Windows. Naming it directly is an AttributeError if the
    Windows-only methods below are ever reached on POSIX, and a type error for any checker run
    there. Every Windows API failure goes through this instead.
    """

    win_error = getattr(ctypes, "WinError", None)
    if win_error is None:
        raise OSError("Windows Job Object operations are not available on this platform")
    return win_error


class _WindowsJob:
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    @classmethod
    def create_kill_on_close(cls) -> _WindowsJob:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _win_error()(ctypes.get_last_error())
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = cls._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            cls._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise _win_error()(error)
        return cls(int(handle))

    def assign(self, pid: int) -> None:
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        process_handle = self._kernel32.OpenProcess(
            self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE, False, pid
        )
        if not process_handle:
            raise _win_error()(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
                raise _win_error()(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process_handle)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0
