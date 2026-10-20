"""Artifact metadata and a deliberately small logging allowlist."""

from typing import Annotated, Literal

from pydantic import Field

from .contracts import Contract
from .provider import TokenUsage

Count = Annotated[int, Field(ge=0, strict=True)]


class StageMetadata(Contract):
    stage: Literal["scenarios", "tests", "repair"]
    backend: Literal["fake", "gemini"]
    model_id: str | None
    prompt_version: str
    run_id: str
    snapshot_id: str
    iteration: Count
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    api_calls: Count
    count_calls: Count
    generation_calls: Count
    generations: Count
    usage: TokenUsage | None = None
    error_code: str | None = None
    last_provider_error_code: str | None = None
    bundle_hash: str | None = None

    def log_fields(self) -> dict[str, str | int | float]:
        # IDs, model names, errors, hashes and all text are artifact-only data.
        return {
            "llm_stage": self.stage,
            "llm_backend": self.backend,
            "llm_duration_seconds": self.duration_seconds,
            "llm_api_calls": self.api_calls,
            "llm_count_calls": self.count_calls,
            "llm_generation_calls": self.generation_calls,
            "llm_generations": self.generations,
        }


SAFE_ERROR_CODES = frozenset(
    [
        "api_budget_exhausted",
        "attempts_exhausted",
        "client_close_failed",
        "context_limit_exceeded",
        "coverage_report_missing",
        "deadline_exceeded",
        "empty_response",
        "fake_invalid_response",
        "fake_invalid_token_count",
        "fake_responses_exhausted",
        "fake_token_counts_exhausted",
        "generation_budget_exhausted",
        "incomplete_response",
        "invalid_budget",
        "invalid_context",
        "invalid_feedback",
        "invalid_iteration",
        "invalid_json",
        "invalid_provider_response",
        "invalid_repair_bundle",
        "invalid_scenarios",
        "invalid_schema",
        "invalid_source_reference",
        "invalid_test_bundle",
        "invalid_token_count",
        "model_mismatch",
        "model_unavailable",
        "provider_failure",
        "provider_forbidden",
        "provider_http_error",
        "provider_invalid_request",
        "provider_quota",
        "provider_refusal",
        "provider_timeout",
        "provider_unauthorized",
        "provider_unavailable",
        "repair_limit_exceeded",
        "repair_unchanged",
        "truncated_response",
        "unexpected_candidates",
        "unexpected_content",
    ]
)
