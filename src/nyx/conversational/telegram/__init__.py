"""The Telegram front-end for NYX.

This is the phone-facing half of the conversational layer (Phase 7 slice 6.4). It reuses the
same :class:`~nyx.conversational.agent.ChatAgent` as ``nyx chat``, so it inherits the one
property that matters: **the language model is the interface, never the judge.** A Telegram
user can ask about findings, file a rebuttal, and request verification. Nothing here can
create a finding, record a disposition, or change a lifecycle state.

Two things this package adds on top of the agent, both ported as *patterns* from The-Agency's
proven bot stack (never vendored):

* **Admission, fail closed.** The bot answers only private chats from an explicit allowlist
  of Telegram user ids. There is no implicit "anyone may talk to it".
* **A per-user rate limit.** A runaway conversation must not be able to spend unbounded model
  calls. The limiter paces use; the allowlist authorises it.

Nothing in here reads or writes NYX directly: every answer goes through the same HTTP client
the CLI chat uses.
"""

from __future__ import annotations
