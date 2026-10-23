"""The unattended watcher core (ADR 0009).

The package is deliberately two modules, because the split is the safety property:

``runner.py``
    One pass over the enabled adapters. Read-only: fetch, ingest through the ordinary
    gateway, criticise, audit. Never files a rebuttal, never records a disposition, never
    touches a lifecycle state.

``loop.py``
    The timer. :func:`run_forever` repeats a cycle every ``interval_seconds`` and yields
    each cycle's outcomes. One bad cycle is recorded, never raised.

A watcher that writes nothing to the audit trail is exactly the thing NYX exists to catch, so
every cycle appends exactly one ``watch.cycle`` entry per adapter -- including the degraded
and failed ones.
"""

from __future__ import annotations

from nyx.watch.loop import run_forever
from nyx.watch.runner import CycleOutcome, WatchCycle

__all__ = ["CycleOutcome", "WatchCycle", "run_forever"]
