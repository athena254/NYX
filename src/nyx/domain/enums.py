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
