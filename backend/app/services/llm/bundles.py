"""Unverified test artifacts; these DTOs never read or write files."""

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, GenerationContext, Text
from .scenarios import ScenarioSet
from .telemetry import StageMetadata


def relative_parts(path: str) -> tuple[str, ...]:
    """Accept canonical POSIX paths only, without platform-specific reinterpretation."""
    parts = tuple(path.split("/"))
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    if (
        any(part in {"", ".", ".."} or part != part.strip() for part in parts)
        or any(char in path for char in "\\:%")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part.endswith(".") for part in parts)
        or any(part.split(".")[0].casefold() in reserved for part in parts)
    ):
        raise ValueError("artifact paths must be canonical relative POSIX paths")
    return parts


class GeneratedFile(Contract):
    path: str
    content: Annotated[str, Field(min_length=1)]
    scenario_ids: Annotated[tuple[Text, ...], Field(min_length=1)]

    @field_validator("scenario_ids")
    @classmethod
    def unique_scenarios(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("scenario links in a file must be unique")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        relative_parts(value)
        if not value.endswith(".py"):
            raise ValueError("only Python test artifacts are allowed")
        return value

    @field_validator("content")
    @classmethod
    def require_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("test content must not be blank")
        return value


class TestBundleDraft(Contract):
    files: Annotated[tuple[GeneratedFile, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_paths(self) -> Self:
        paths = [file.path.casefold() for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        return self


def compute_bundle_hash(files: tuple[GeneratedFile, ...]) -> str:
    payload = [
        {"path": file.path, "content": file.content, "scenario_ids": sorted(file.scenario_ids)}
        for file in sorted(files, key=lambda file: file.path)
    ]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestBundle(TestBundleDraft):
    """Service-bound artifacts, with no claims about execution or coverage."""

    metadata: StageMetadata | None = None
    schema_version: Literal["1"]
    run_id: Text
    snapshot_id: Text
    bundle_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    prompt_version: Text | None = None
    omitted_source_ids: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def verify_hash(self) -> Self:
        if self.bundle_hash != compute_bundle_hash(self.files):
            raise ValueError("bundle hash does not match artifacts")
        return self

    @classmethod
    def from_context(
        cls,
        draft: TestBundleDraft,
        context: GenerationContext,
        scenarios: ScenarioSet,
        *,
        prompt_version: str | None = None,
        omitted_source_ids: tuple[str, ...] = (),
    ) -> Self:
        if (scenarios.run_id, scenarios.snapshot_id) != (
            context.run_id,
            context.snapshot.snapshot_id,
        ):
            raise ValueError("scenarios belong to a different run or snapshot")
        root = relative_parts(context.generated_tests_dir)
        protected = [
            tuple(part.casefold() for part in relative_parts(path))
            for path in context.protected_paths
        ]
        known_ids = {scenario.scenario_id for scenario in scenarios.scenarios}
        for file in draft.files:
            parts = relative_parts(file.path)
            if len(parts) <= len(root) or parts[: len(root)] != root:
                raise ValueError("artifact is outside the generated test directory")
            folded = tuple(part.casefold() for part in parts)
            if any(folded[: len(path)] == path for path in protected):
                raise ValueError("artifact overlaps a protected path")
            if set(file.scenario_ids) - known_ids:
                raise ValueError("artifact references an unknown scenario")
        if {scenario_id for file in draft.files for scenario_id in file.scenario_ids} != known_ids:
            raise ValueError("not all scenarios are mapped to artifacts")
        return cls(
            **draft.model_dump(),
            schema_version="1",
            run_id=context.run_id,
            snapshot_id=context.snapshot.snapshot_id,
            bundle_hash=compute_bundle_hash(draft.files),
            prompt_version=prompt_version,
            omitted_source_ids=omitted_source_ids,
        )
