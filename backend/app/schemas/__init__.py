# Pydantic v2 schemas for AI-Merge-Assistant (Schema Version T1)

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION: Literal["T1"] = "T1"


class SchemaVersionMixin(BaseModel):
    """Mixin to enforce schema_version = T1 on all DTOs."""
    schema_version: Literal["T1"] = Field(default=SCHEMA_VERSION, frozen=True)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v != SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema version: {v}. Expected {SCHEMA_VERSION}")
        return v


class SourceType(str, Enum):
    GITHUB = "github"
    UPLOAD = "upload"


class ExecutionState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    GENERATING = "generating"
    VALIDATING = "validating"
    RUNNING = "running"
    CHECKING_COVERAGE = "checking_coverage"
    IMPROVING = "improving"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Verdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class Freshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"


class MergeStatus(str, Enum):
    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    COMPLETED = "completed"
    CONFLICT = "conflict"


class Mode(str, Enum):
    DEFAULT = "default"
    LAZY = "lazy"
    MANUAL = "manual"
    AUTO = "auto"


class RunnerMode(str, Enum):
    COLLECT = "collect"
    TEST = "test"


class RunnerStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class GitHubOperationType(str, Enum):
    READ_DIFF = "read_diff"
    PUSH_TESTS = "push_tests"
    CREATE_PR = "create_pr"
    MERGE_PR = "merge_pr"
    CHECK_STATUS = "check_status"
    VERIFY_WEBHOOK = "verify_webhook"


class GitHubOperationStatus(str, Enum):
    SUCCESS = "success"
    CONFLICT = "conflict"
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"


class RequirementTag(str, Enum):
    REQUIREMENTS = "requirements"
    INFERRED = "inferred"


class ErrorCategory(str, Enum):
    MODEL = "model"
    ENVIRONMENT = "environment"
    TESTS = "tests"
    QUOTA = "quota"
    RIGHTS = "rights"
    STALE = "stale"


class SourceSnapshot(SchemaVersionMixin):
    source_type: SourceType
    run_id: UUID
    snapshot_id: UUID
    repo_id: UUID | None = None
    repo_owner: str | None = Field(None, min_length=1, max_length=255)
    repo_name: str | None = Field(None, min_length=1, max_length=255)
    source_sha: str = Field(..., min_length=7, max_length=40, pattern=r"^[a-f0-9]{7,40}$")
    target_sha: str = Field(..., min_length=7, max_length=40, pattern=r"^[a-f0-9]{7,40}$")
    diff_text: str | None = None
    diff_artifact_path: str | None = None
    content_reference: str
    content_hash: str = Field(..., min_length=32, max_length=128)
    environment_profile: str = Field(..., min_length=1, max_length=128)

    @field_validator("repo_owner", "repo_name", mode="after")
    @classmethod
    def _validate_github_fields(cls, v: str | None, info) -> str | None:
        if info.data.get("source_type") == SourceType.GITHUB and v is None:
            raise ValueError("repo_owner and repo_name are required for github source_type")
        return v

    @field_validator("diff_text", "diff_artifact_path", mode="after")
    @classmethod
    def _validate_diff_present(cls, v: str | None, info) -> str | None:
        other_field = "diff_artifact_path" if info.field_name == "diff_text" else "diff_text"
        if info.data.get("source_type") == SourceType.GITHUB and not v and not info.data.get(other_field):
            raise ValueError("At least one of diff_text or diff_artifact_path must be provided for github")
        return v


class GenerationContext(SchemaVersionMixin):
    source_snapshot: SourceSnapshot
    mode: Mode
    description: str | None = Field(None, max_length=10000)
    requirements_text: str | None = Field(None, max_length=50000)
    diff_text: str | None = None
    code_fragments: list[str] = Field(default_factory=list)
    existing_tests: list[str] = Field(default_factory=list)
    source_ids: list[UUID] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    dependency_profile: str = Field(..., min_length=1, max_length=128)

    @field_validator("code_fragments", mode="after")
    @classmethod
    def _validate_code_fragments(cls, v: list[str]) -> list[str]:
        if len(v) > 50:
            raise ValueError("code_fragments must not exceed 50 items")
        for frag in v:
            if len(frag) > 20000:
                raise ValueError("Each code fragment must not exceed 20000 characters")
        return v

    @field_validator("allowed_paths", mode="after")
    @classmethod
    def _validate_allowed_paths(cls, v: list[str]) -> list[str]:
        for path in v:
            if path.startswith("/") or ".." in path:
                raise ValueError(f"allowed_paths must be safe relative paths: {path}")
        return v


class ScenarioItem(SchemaVersionMixin):
    scenario_id: UUID
    source_refs: list[UUID]
    requirement_tag: RequirementTag
    gherkin_text: str = Field(..., min_length=1, max_length=20000)


class ScenarioSet(SchemaVersionMixin):
    set_id: UUID
    run_id: UUID
    snapshot_id: UUID
    scenarios: list[ScenarioItem] = Field(..., min_length=1)
    prompt_model: str = Field(..., min_length=1, max_length=256)
    prompt_version: str = Field(..., min_length=1, max_length=32)
    usage: Literal["known", "unknown"] = "unknown"
    set_hash: str = Field(..., min_length=32, max_length=128)


class TestItem(SchemaVersionMixin):
    test_path: str = Field(..., min_length=1, max_length=512)
    test_text: str = Field(..., min_length=1, max_length=50000)
    linked_scenario_ids: list[UUID] = Field(default_factory=list)

    @field_validator("test_path", mode="after")
    @classmethod
    def _validate_test_path(cls, v: str) -> str:
        if v.startswith("/") or ".." in v:
            raise ValueError(f"test_path must be a safe relative path: {v}")
        return v


class TestBundle(SchemaVersionMixin):
    bundle_id: UUID
    run_id: UUID
    snapshot_id: UUID
    tests: list[TestItem] = Field(..., min_length=1)
    bundle_hash: str = Field(..., min_length=32, max_length=128)
    prompt_model: str
    prompt_version: str
    usage: Literal["known", "unknown"] = "unknown"


class RunnerJob(SchemaVersionMixin):
    job_id: UUID
    run_id: UUID
    snapshot_id: UUID
    attempt_id: UUID
    test_bundle_hash: str = Field(..., min_length=32, max_length=128)
    artifacts: list[str] = Field(default_factory=list)
    allowed_profile: str = Field(..., min_length=1, max_length=128)
    cpu_limit: int = Field(..., ge=1, le=8)
    memory_limit_mb: int = Field(..., ge=128, le=8192)
    timeout_seconds: int = Field(..., ge=30, le=600)
    pid_limit: int = Field(..., ge=10, le=500)
    disk_limit_mb: int = Field(..., ge=100, le=10000)
    mode: RunnerMode
    fixed_command: str | None = Field(None, max_length=1024)

    @field_validator("artifacts", mode="after")
    @classmethod
    def _validate_artifacts(cls, v: list[str]) -> list[str]:
        for path in v:
            if path.startswith("/") or ".." in path:
                raise ValueError(f"artifact path must be safe relative: {path}")
        return v

    @field_validator("fixed_command", mode="after")
    @classmethod
    def _validate_fixed_command(cls, v: str | None) -> str | None:
        if v is not None:
            forbidden = ["curl", "wget", "bash -c", "eval", "exec", "sh -c", "python -c"]
            for f in forbidden:
                if f in v:
                    raise ValueError(f"fixed_command contains forbidden substring: {f}")
        return v


class ExecutionReport(SchemaVersionMixin):
    job_id: UUID
    run_id: UUID
    snapshot_id: UUID
    attempt_id: UUID
    test_bundle_hash: str = Field(..., min_length=32, max_length=128)
    image_digest: str = Field(..., min_length=32, max_length=128)
    status: RunnerStatus
    exit_code: int | None = Field(None, ge=-1, le=255)
    collect_result: dict[str, Any] | None = None
    test_result: dict[str, Any] | None = None
    bounded_logs: list[str] = Field(default_factory=list)
    log_limit_lines: int = Field(default=500, ge=1, le=5000)
    raw_coverage_reports: list[str] = Field(default_factory=list)
    failure_reason: str | None = Field(None, max_length=2000)
    timeout_reason: str | None = Field(None, max_length=2000)

    @field_validator("bounded_logs", mode="after")
    @classmethod
    def _validate_bounded_logs(cls, v: list[str], info) -> list[str]:
        limit = info.data.get("log_limit_lines", 500)
        total_lines = sum(log.count("\n") + 1 for log in v)
        if total_lines > limit:
            raise ValueError(f"bounded_logs exceed log_limit_lines ({limit})")
        for log in v:
            if len(log) > 2000:
                raise ValueError("Each log entry must not exceed 2000 characters")
        return v


class ValidationFeedback(SchemaVersionMixin):
    snapshot_id: UUID
    test_bundle_hash: str = Field(..., min_length=32, max_length=128)
    structured_errors: list[dict[str, Any]] = Field(default_factory=list)
    uncovered_changed_lines: list[int] = Field(default_factory=list)
    iteration: int = Field(..., ge=1, le=10)
    remaining_budget: int = Field(..., ge=0, le=10)

    @field_validator("structured_errors", mode="after")
    @classmethod
    def _validate_structured_errors(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for err in v:
            if "type" not in err or err["type"] not in [c.value for c in ErrorCategory]:
                raise ValueError(f"structured_errors must have valid type: {err}")
            if "path" not in err or "message" not in err:
                raise ValueError("structured_errors must contain path and message")
        return v

    @field_validator("uncovered_changed_lines", mode="after")
    @classmethod
    def _validate_uncovered_lines(cls, v: list[int]) -> list[int]:
        if any(line <= 0 for line in v):
            raise ValueError("Line numbers must be positive")
        if v != sorted(v):
            raise ValueError("uncovered_changed_lines must be sorted ascending")
        return v


class MergeDecision(SchemaVersionMixin):
    owner_user_id: UUID
    owner_github_login: str = Field(..., min_length=1, max_length=255)
    run_id: UUID
    attempt_id: UUID
    verified_source_sha: str = Field(..., min_length=7, max_length=40)
    verified_target_sha: str = Field(..., min_length=7, max_length=40)
    verified_test_bundle_hash: str = Field(..., min_length=32, max_length=128)
    allowed: bool
    denial_reasons: list[str] = Field(default_factory=list)
    mode: Mode
    server_created_at: str  # ISO-8601, set by server only


class GitHubOperationResult(SchemaVersionMixin):
    operation_type: GitHubOperationType
    repo_owner: str | None = Field(None, min_length=1, max_length=255)
    repo_name: str | None = Field(None, min_length=1, max_length=255)
    branch_name: str | None = Field(None, min_length=1, max_length=255)
    pr_number: int | None = Field(None, ge=1)
    expected_sha: str | None = Field(None, min_length=7, max_length=40)
    actual_sha: str | None = Field(None, min_length=7, max_length=40)
    status: GitHubOperationStatus
    safe_reason: str | None = Field(None, max_length=2000)
    reconciliation_id: UUID


class CoverageReportDTO(SchemaVersionMixin):
    """Normalized coverage report produced by CoverageCalculator."""
    attempt_id: UUID
    run_id: UUID
    covered_changed_lines: int = Field(ge=0)
    executable_changed_lines: int = Field(ge=0)
    coverage_percentage: float | None = None  # None when executable_changed_lines == 0
    covered_files: list[str] = Field(default_factory=list)
    uncovered_changed_lines: list[int] = Field(default_factory=list)
    raw_coverage_path: str | None = None
    coverage_verdict: Verdict
    error_category: ErrorCategory | None = None
    error_message: str | None = None


class MergeGateInput(SchemaVersionMixin):
    """Input for MergeGate decision."""
    run_id: UUID
    attempt_id: UUID
    execution_state: ExecutionState
    verdict: Verdict
    freshness: Freshness
    merge_status: MergeStatus
    coverage_report: CoverageReportDTO
    test_bundle_hash: str
    source_sha: str
    target_sha: str
    owner_user_id: UUID
    owner_github_login: str
    auto_merge_enabled: bool
    target_branch_protected: bool
    user_has_write_permission: bool
    github_app_has_write_permission: bool


class MergeGateOutput(SchemaVersionMixin):
    """Output from MergeGate."""
    merge_decision: MergeDecision
    allowed: bool
    denial_reasons: list[str]


class GitHubActionRecord(SchemaVersionMixin):
    """Record of a GitHub write operation for idempotency."""
    id: UUID
    user_id: UUID
    run_id: UUID | None = None
    operation_type: GitHubOperationType
    repo_owner: str | None = None
    repo_name: str | None = None
    branch_name: str | None = None
    pr_number: int | None = None
    expected_sha: str | None = None
    actual_sha: str | None = None
    status: GitHubOperationStatus
    safe_reason: str | None = None
    reconciliation_id: UUID
    initiator_user_id: UUID
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601


class WebhookDeliveryRecord(SchemaVersionMixin):
    delivery_id: str
    event_type: str
    payload_hash: str
    signature_valid: bool
    processed: bool = False
    processed_at: str | None = None
    created_at: str


class OrchestratorTransition(SchemaVersionMixin):
    """Valid state transition for RunOrchestrator."""
    from_state: ExecutionState
    to_state: ExecutionState
    trigger: str


# Valid transitions for RunOrchestrator (T1 states from team-guide.md)
VALID_TRANSITIONS: dict[ExecutionState, set[ExecutionState]] = {
    ExecutionState.QUEUED: {ExecutionState.PREPARING, ExecutionState.CANCELLED},
    ExecutionState.PREPARING: {ExecutionState.GENERATING, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.GENERATING: {ExecutionState.VALIDATING, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.VALIDATING: {ExecutionState.RUNNING, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.RUNNING: {ExecutionState.CHECKING_COVERAGE, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.CHECKING_COVERAGE: {ExecutionState.IMPROVING, ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.IMPROVING: {ExecutionState.VALIDATING, ExecutionState.RUNNING},
    ExecutionState.COMPLETED: set(),  # Terminal
    ExecutionState.FAILED: set(),    # Terminal
    ExecutionState.CANCELLED: set(), # Terminal
}


MAX_IMPROVING_ITERATIONS = 2  # T3: initial + 2 improvements = 3 total generations
LLM_STAGE_TIMEOUT_SECONDS = 120
LLM_REQUEST_TIMEOUT_SECONDS = 60
MAX_TRANSPORT_ATTEMPTS = 3
