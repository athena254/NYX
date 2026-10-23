"""The watcher's timer: repeat a cycle until told to stop (ADR 0009).

``nyx watch`` is a CLI command, not a background thread inside ``serve``. This module is the
whole of the loop: it calls :meth:`WatchCycle.run_once`, yields the outcomes, sleeps, and
repeats. It does not daemonise itself -- process supervision is a host concern, and a service
that re-implements it will do it worse (ADR 0009, "Consequences").

Two properties this module exists to make enforceable:

* **One bad cycle never kills the loop.** :meth:`WatchCycle.run_once` already cannot raise, and
  this loop catches anything anyway: a cycle that raises is logged and followed by the sleep,
  so the watcher keeps watching rather than exiting on the first surprise. A watcher that dies
  on the first network blip is not a watcher.
* **A set ``stop_event`` ends the loop promptly.** The sleep is interruptible, so a supervisor
  that sets the event does not wait out a full interval to notice.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

from nyx.watch.runner import CycleOutcome, WatchCycle

#: How long the loop waits before re-checking a stop signal during a sleep. Short enough that a
#: supervisor's stop lands within a second, long enough that a short interval does not spin.
_SLEEP_GRANULARITY_SECONDS = 0.5


def run_forever(
    cycle: WatchCycle,
    *,
    interval_seconds: float,
    stop_event: threading.Event | None = None,
) -> Iterator[list[CycleOutcome]]:
    """Yield one :class:`CycleOutcome` list per cycle, forever, or until ``stop_event`` is set.

    The interval is measured **between** cycles, not added to a cycle's duration: a slow cycle
    followed by a full interval would drift, and the operator's configured cadence is the
    cadence they get.

    A set event ends the loop **before** the next cycle, so a supervisor that stops the watcher
    gets no further work and no wait. The stop check is at the top of the loop, which is what
    makes a pre-set event return immediately with zero cycles.

    Never raises for anything a cycle can do. ``KeyboardInterrupt`` and ``SystemExit`` are
    re-raised: a process that is being stopped must be allowed to stop, and swallowing them
    would turn Ctrl-C into a hung supervisor.
    """

    logger = logging.getLogger("nyx.watch")
    while True:
        if stop_event is not None and stop_event.is_set():
            return

        try:
            outcomes = cycle.run_once()
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the loop
            logger.error(
                "watch: cycle raised %s; continuing after the interval",
                type(exc).__name__,
            )
            outcomes = []
        yield outcomes

        _sleep(interval_seconds, stop_event=stop_event, logger=logger)


def _sleep(seconds: float, *, stop_event: threading.Event | None, logger: logging.Logger) -> None:
    """Sleep for ``seconds``, waking early if ``stop_event`` is set.

    The loop is interruptible rather than one long ``time.sleep``: a stop signal must end the
    loop promptly, which ADR 0009 requires and a single blocking sleep cannot guarantee.
    """

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_event is None:
            time.sleep(max(0.0, deadline - time.monotonic()))
            return
        # ``Event.wait`` is interruptible and returns immediately once set, so this is both
        # the timer and the stop check.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop_event.wait(timeout=min(_SLEEP_GRANULARITY_SECONDS, remaining))


__all__ = ["run_forever"]
