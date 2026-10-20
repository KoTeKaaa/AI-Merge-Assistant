"""Normalized feedback supplied by the orchestrator; no report parsing or quality verdicts."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .bundles import TestBundle, relative_parts
from .contracts import Contract, GenerationContext, Text

Nonnegative = Annotated[int, Field(ge=0, strict=True)]
Line = Annotated[int, Field(gt=0, strict=True)]


class ValidationIssue(Contract):
    kind: Literal["syntax", "import", "assertion", "existing_test", "environment", "static"]
    message: Text = Field(repr=False)
    path: Text | None = None
    line: Line | None = None

    @model_validator(mode="after")
    def validate_location(self) -> Self:
        if self.path is not None:
            relative_parts(self.path)
        if self.line is not None and self.path is None:
            raise ValueError("issue line requires a path")
        return self


class UncoveredLines(Contract):
    path: Text
    lines: Annotated[tuple[Line, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_lines(self) -> Self:
        relative_parts(self.path)
        if len(self.lines) != len(set(self.lines)):
            raise ValueError("uncovered lines must be unique")
        return self


class CoverageFeedback(Contract):
    executable_changed_lines: Nonnegative
    covered_changed_lines: Nonnegative
    uncovered: tuple[UncoveredLines, ...] = ()

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        paths = [file.path for file in self.uncovered]
        missing = sum(len(file.lines) for file in self.uncovered)
        if len(paths) != len(set(paths)) or (
            self.covered_changed_lines + missing != self.executable_changed_lines
        ):
            raise ValueError("coverage counters and uncovered lines are inconsistent")
        return self


class ValidationFeedback(Contract):
    schema_version: Literal["1"]
    run_id: Text
    snapshot_id: Text
    bundle_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    iteration: Line
    reason: Literal[
        "validation_error", "collection_error", "test_failure", "coverage_gap", "coverage_missing"
    ]
    issues: tuple[ValidationIssue, ...] = ()
    coverage: CoverageFeedback | None = None

    @model_validator(mode="after")
    def consistent_reason(self) -> Self:
        if (
            self.reason in {"validation_error", "collection_error", "test_failure"}
            and not self.issues
        ):
            raise ValueError("error feedback requires normalized issues")
        if self.reason == "coverage_missing" and self.coverage is not None:
            raise ValueError("missing coverage cannot contain a report")
        if self.reason == "coverage_gap" and (self.coverage is None or not self.coverage.uncovered):
            raise ValueError("coverage gap requires uncovered measured lines")
        return self

    def validate_for(self, context: GenerationContext, bundle: TestBundle) -> Self:
        identity = (context.run_id, context.snapshot.snapshot_id, bundle.bundle_hash)
        if (self.run_id, self.snapshot_id, self.bundle_hash) != identity or (
            bundle.run_id,
            bundle.snapshot_id,
        ) != identity[:2]:
            raise ValueError("feedback belongs to a different run, snapshot or bundle")
        measured = {file.path: set(file.lines) for file in context.changed_lines}
        generated = {file.path: len(file.content.splitlines()) for file in bundle.files}
        if any(
            issue.path in generated
            and issue.line is not None
            and issue.line > generated[issue.path]
            for issue in self.issues
        ):
            raise ValueError("issue line is outside generated content")
        for path in measured:
            relative_parts(path)
        if self.coverage is not None:
            if self.coverage.executable_changed_lines != sum(map(len, measured.values())):
                raise ValueError("coverage does not match measured changed lines")
            if any(
                set(file.lines) - measured.get(file.path, set()) for file in self.coverage.uncovered
            ):
                raise ValueError("uncovered line is outside measured changes")
        return self
