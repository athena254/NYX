"""The ground-truth evidence layer.

Four kinds of evidence, kept apart on purpose:

* **Claim** -- what an agent or system asserts (``nyx.domain.claims.Claim``).
* **Observation** -- what NYX itself gathered through a probe (``nyx.domain.observation``).
* **Artifact** -- a stored file or bundle, identified by content hash (``EvidenceItem.content_hash``
  with ``EvidenceKind.ARTIFACT_DIGEST``).
* **Authorisation** -- who approved what action, for what scope, until when
  (``nyx.domain.authorisation``).

The separation is the whole point. A claim that a booking was made is not the booking existing in
the database; keeping the two in different records is what makes the second checkable
independently of the first.

Submodules:

* :mod:`nyx.evidence.probes` -- the ``Probe`` protocol and the five built-in kinds.
* :mod:`nyx.evidence.verification` -- how strong a piece of support is, in one place.
* :mod:`nyx.evidence.claims` -- deterministic extraction of typed claims from transcripts.
* :mod:`nyx.evidence.registry` -- registration, budgets, and the read-only default.
"""

from __future__ import annotations
