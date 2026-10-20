"""Proposed replacements or an explicit explanation that tests cannot fix the issue."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .bundles import TestBundle, TestBundleDraft
from .contracts import Contract, Text
from .feedback import ValidationFeedback


class RepairInput(Contract):
    previous_bundle: TestBundle = Field(repr=False)
    feedback: ValidationFeedback = Field(repr=False)


class RepairDecision[BundleT: Contract](Contract):
    action: Literal["replace", "cannot_repair"]
    bundle: BundleT | None = Field(default=None, repr=False)
    explanation: Text | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def require_decision(self) -> Self:
        if self.action == "replace" and self.bundle is None:
            raise ValueError("replacement requires a complete bundle")
        if self.action == "cannot_repair" and (self.bundle is not None or self.explanation is None):
            raise ValueError("cannot_repair requires an explanation and no bundle")
        return self


RepairDraft = RepairDecision[TestBundleDraft]


class RepairResult(RepairDecision[TestBundle]):
    schema_version: Literal["1"]
    run_id: Text
    snapshot_id: Text
    previous_bundle_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    iteration: Annotated[int, Field(gt=0, strict=True)]

    @model_validator(mode="after")
    def matching_bundle(self) -> Self:
        if self.bundle is not None and (self.bundle.run_id, self.bundle.snapshot_id) != (
            self.run_id,
            self.snapshot_id,
        ):
            raise ValueError("replacement bundle belongs to a different run or snapshot")
        return self
