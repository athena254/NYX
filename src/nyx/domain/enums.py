"""Shared enumerations for the NYX domain model.

Enumerations are defined once here so that domain models, storage, critics, findings,
disputes, and later phases all use the same wire values. Values are the canonical strings
stored in the database and serialised to JSON.
"""

from enum import StrEnum


class SystemKind(StrEnum):
    AGENT = "agent"
    SERVICE = "service"
    REPOSITORY = "repository"
    PIPELINE = "pipeline"
    MODEL = "model"
    HUMAN = "human"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    LOCAL = "local"


class AdapterKind(StrEnum):
    HTTP = "http"
    CLI = "cli"
    FILE = "file"
    OPENTELEMETRY = "opentelemetry"
    CUSTOM = "custom"


class SystemStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class PrincipalKind(StrEnum):
    SYSTEM = "system"
    PRINCIPAL = "principal"
    AGENT = "agent"
    TOOL = "tool"
    HUMAN = "human"
    SERVICE = "service"
    ARTIFACT = "artifact"
    UNKNOWN = "unknown"


class TrustClass(StrEnum):
    UNVERIFIED = "unverified"
    OBSERVED = "observed"
    TOOL_VERIFIED = "tool_verified"
    SIGNED_ATTESTATION = "signed_attestation"


class RetentionClass(StrEnum):
    EPHEMERAL = "ephemeral"
    STANDARD = "standard"
    EXTENDED = "extended"
    LEGAL_HOLD = "legal_hold"
    DERIVED_METADATA = "derived_metadata"


class ClaimType(StrEnum):
    FACTUAL = "factual"
    CAUSAL = "causal"
    PREDICTIVE = "predictive"
    POLICY = "policy"
    COMPLETION = "completion"
    SECURITY = "security"
    QUALITY = "quality"
    OTHER = "other"


class ClaimStatus(StrEnum):
    UNASSESSED = "unassessed"
    SUPPORTED = "supported"
    CHALLENGED = "challenged"
    DISPUTED = "disputed"
    VERIFIED_WITHIN_SCOPE = "verified_within_scope"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class EvidenceKind(StrEnum):
    CI_TEST_RESULT = "ci_test_result"
    TEST_RESULT = "test_result"
    LOG = "log"
    TRACE = "trace"
    SOURCE_FILE = "source_file"
    COMMIT = "commit"
    ARTIFACT_DIGEST = "artifact_digest"
    ATTESTATION = "attestation"
    MEASUREMENT = "measurement"
    POLICY_EVALUATION = "policy_evaluation"
    REVIEW_RECORD = "review_record"
    DEPLOYMENT_RECORD = "deployment_record"
    RUNTIME_OBSERVATION = "runtime_observation"
    MODEL_ASSESSMENT = "model_assessment"
    EXTERNAL_REFERENCE = "external_reference"
    OTHER = "other"


class VerificationStrength(StrEnum):
    """How strongly a proposition is supported by what backs it.

    Ordered from weakest to strongest, and **shared across every critic** so that one
    critic's "verified" and another's mean the same thing. The ordering is the point: a
    completion claim needs at least ``RERUN_BY_NYX`` (SPEC §8, C-001 as upgraded), and the
    comparison is a simple index test rather than a judgement.

    * ``NONE`` -- nothing backs the proposition. An agent said it; that is all.
    * ``SELF_REPORTED`` -- the claimant's own record is the only support. The claimant
      verifying itself is **not** verification (ADR 0005: evidence is not proof).
    * ``RERUN_BY_NYX`` -- NYX re-executed the check itself at the current subject version.
      Independent of the claimant, but still reading the same artifact.
    * ``INDEPENDENTLY_OBSERVED_END_STATE`` -- NYX observed the real end state of the
      environment: the row exists, the file is on disk, the service answers. Strongest.
    """

    NONE = "none"
    SELF_REPORTED = "self_reported"
    RERUN_BY_NYX = "rerun_by_nyx"
    INDEPENDENTLY_OBSERVED_END_STATE = "independently_observed_end_state"


class RiskAction(StrEnum):
    """The risk taxonomy C-006 classifies an action against.

    Seven categories, named by the brief and kept deliberately small. An action that maps to
    none of them is **not** high-impact: inventing a category would manufacture findings, and a
    false accusation is worse than a missed verb. C-006's existing token set is the *detection*
    mechanism; this enum is the *classification* the detection maps into.

    * ``DELETE`` -- remove a record, file, or resource.
    * ``OVERWRITE_PRODUCTION_STATE`` -- write to a production database, table, or store.
    * ``DEPLOY`` -- release or roll back a deployment.
    * ``SPEND`` -- commit money or quota.
    * ``EXTERNAL_SEND`` -- transmit data outside the trust boundary (email, webhook, message).
    * ``PERMISSION_CHANGE`` -- grant, revoke, or alter a permission or capability.
    * ``SECRET_ACCESS`` -- read a credential, token, or key.
    """

    DELETE = "delete"
    OVERWRITE_PRODUCTION_STATE = "overwrite_production_state"
    DEPLOY = "deploy"
    SPEND = "spend"
    EXTERNAL_SEND = "external_send"
    PERMISSION_CHANGE = "permission_change"
    # S105 is the hardcoded-password rule, and this string is a taxonomy label, not a credential:
    # it names the category of action that needs authorisation. The value is the wire name, so
    # splitting it to satisfy the linter would make the label unreadable for no security gain.
    SECRET_ACCESS = "secret_access"  # noqa: S105


class ProbeKind(StrEnum):
    """The five probe kinds the ground-truth layer ships with.

    A probe kind is a *method*, not a permission. ``read_only`` on the probe result is what
    governs safety, and a probe that reports ``read_only=False`` may only run behind the
    mutating-probe gate (``docs/safety-model.md``).
    """

    STATE_CHECK = "state_check"
    REEXECUTION = "reexecution"
    TOOL_CALL_VERIFICATION = "tool_call_verification"
    SIDE_EFFECT = "side_effect"
    LOG_METRIC_QUERY = "log_metric_query"


class ProbeOutcome(StrEnum):
    """What one probe established.

    ``ERROR`` and ``SKIPPED`` are distinct on purpose: a probe that could not run is not a
    probe that found nothing, and conflating them would let a broken collector look clean.
    """

    OBSERVED = "observed"
    NOT_OBSERVED = "not_observed"
    ERROR = "error"
    SKIPPED = "skipped"


class IntegrityMethod(StrEnum):
    NONE = "none"
    HASH = "hash"
    SIGNED = "signed"
    ATTESTED = "attested"
    EXTERNAL = "external"


class VerificationResult(StrEnum):
    NOT_CHECKED = "not_checked"
    VERIFIED = "verified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class CriticType(StrEnum):
    DETERMINISTIC = "deterministic"
    TOOL = "tool"
    LLM = "llm"
    HUMAN = "human"


class CriticResultStatus(StrEnum):
    FINDING = "finding"
    NO_FINDING = "no_finding"
    ERROR = "error"
    SKIPPED = "skipped"
    INCONCLUSIVE = "inconclusive"


class FindingCategory(StrEnum):
    CONTRADICTION = "contradiction"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    POLICY_VIOLATION = "policy_violation"
    SECURITY_RISK = "security_risk"
    QUALITY_RISK = "quality_risk"
    DRIFT = "drift"
    MISSING_VERIFICATION = "missing_verification"
    PROVENANCE_GAP = "provenance_gap"
    OTHER = "other"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    DETECTED = "detected"
    EVIDENCE_PENDING = "evidence_pending"
    CHALLENGED = "challenged"
    REBUTTAL_PENDING = "rebuttal_pending"
    VERIFICATION_PENDING = "verification_pending"
    CONFIRMED = "confirmed"
    PARTIALLY_CONFIRMED = "partially_confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    REMEDIATION_PENDING = "remediation_pending"
    FIXED_PENDING_VERIFICATION = "fixed_pending_verification"
    CLOSED = "closed"
    ACCEPTED_RISK = "accepted_risk"
    DUPLICATE = "duplicate"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class DispositionOutcome(StrEnum):
    CONFIRMED = "confirmed"
    PARTIALLY_CONFIRMED = "partially_confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    ACCEPTED_RISK = "accepted_risk"
    DUPLICATE = "duplicate"
    WITHDRAWN = "withdrawn"


class RebuttalRequestedOutcome(StrEnum):
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"
    PARTIALLY_CONFIRMED = "partially_confirmed"
    INCONCLUSIVE = "inconclusive"
    ACCEPTED_RISK = "accepted_risk"
    NO_CHANGE = "no_change"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    ERROR = "error"


class VerificationJobStatus(StrEnum):
    """The lifecycle of an independent verification run (SPEC §5.7, §7 step 8).

    ``refused`` is a real outcome, not an error: a job whose check is not on the allowlist never
    runs. Distinguishing it keeps "we declined to run this" from "we ran it and it failed".
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"


class VerificationOutcome(StrEnum):
    """What an independent verification run established about the proposition."""

    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
