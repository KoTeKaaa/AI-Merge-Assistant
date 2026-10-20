"""Versioned, SDK-independent DTOs for prepared generation input."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Mode = Literal["default", "lazy"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    def __repr_args__(self):
        return []


class GitHubSnapshot(Contract):
    kind: Literal["github"]
    snapshot_id: Text
    content_hash: Text
    repository_id: Text
    source_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    target_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class UploadSnapshot(Contract):
    kind: Literal["upload"]
    snapshot_id: Text
    content_hash: Text


class PreparedSource(Contract):
    source_id: Text
    kind: Literal["description", "requirements", "diff", "code", "existing_test"]
    text: Annotated[str, StringConstraints(min_length=1)]
    path: Text | None = None
    optional: Annotated[bool, Field(strict=True)] = False
    start_line: Annotated[int, Field(gt=0, strict=True)] = 1

    @model_validator(mode="after")
    def require_content(self) -> Self:
        if not self.text.strip():
            raise ValueError("source text must not be blank")
        if self.optional and self.kind not in {"code", "existing_test"}:
            raise ValueError("only supporting code and existing tests may be optional")
        return self


class EnvironmentProfile(Contract):
    profile_id: Text
    python_version: Literal["3.13"]
    dependencies: tuple[Text, ...] = ()


class ChangedLines(Contract):
    path: Text
    source_id: Text
    lines: Annotated[tuple[Annotated[int, Field(gt=0, strict=True)], ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_lines(self) -> Self:
        if len(self.lines) != len(set(self.lines)):
            raise ValueError("changed lines must be unique")
        return self


class GenerationContext(Contract):
    schema_version: Literal["1"]
    run_id: Text
    snapshot: Annotated[GitHubSnapshot | UploadSnapshot, Field(discriminator="kind")]
    mode: Mode
    sources: Annotated[tuple[PreparedSource, ...], Field(min_length=1)]
    environment: EnvironmentProfile
    generated_tests_dir: Text
    protected_paths: tuple[Text, ...] = ()
    changed_lines: tuple[ChangedLines, ...] = ()

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        ids = [source.source_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source IDs must be unique")
        kinds = {source.kind for source in self.sources}
        if self.mode == "default" and not kinds.intersection({"description", "requirements"}):
            raise ValueError("default mode requires description or requirements")
        if self.snapshot.kind == "github" and "diff" not in kinds:
            raise ValueError("GitHub input requires a prepared diff")
        if self.snapshot.kind == "upload":
            if self.mode != "default" or "requirements" not in kinds:
                raise ValueError("upload requires default mode and extracted requirements")
        paths = [file.path for file in self.changed_lines]
        if len(paths) != len(set(paths)):
            raise ValueError("changed file paths must be unique")
        sources = {source.source_id: source for source in self.sources}
        for file in self.changed_lines:
            source = sources.get(file.source_id)
            if source is None or source.kind != "code" or source.path != file.path:
                raise ValueError("changed lines require a matching prepared code source")
            end_line = source.start_line + len(source.text.splitlines()) - 1
            if any(line < source.start_line or line > end_line for line in file.lines):
                raise ValueError("changed line is outside its prepared fragment")
        return self
