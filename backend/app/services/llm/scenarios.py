"""Scenario response schema and context binding for the service boundary."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .contracts import Contract, GenerationContext, Text

Steps = Annotated[tuple[Text, ...], Field(min_length=1)]


class Scenario(Contract):
    scenario_id: Text
    given: Steps
    when: Steps
    then: Steps
    source_ids: Annotated[tuple[Text, ...], Field(min_length=1)]


class ScenarioDraftSet(Contract):
    """The model may describe expectations, but cannot assign their provenance."""

    scenarios: Annotated[tuple[Scenario, ...], Field(min_length=1)]
    ambiguities: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def unique_scenario_ids(self) -> Self:
        ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario IDs must be unique")
        return self


class ScenarioSet(ScenarioDraftSet):
    """Service result, never the schema used to parse a model response."""

    schema_version: Literal["1"]
    run_id: Text
    snapshot_id: Text
    expectation_origin: Literal["requirements", "inferred"]
    prompt_version: Text | None = None
    omitted_source_ids: tuple[Text, ...] = ()

    @classmethod
    def from_context(
        cls,
        draft: ScenarioDraftSet,
        context: GenerationContext,
        *,
        prompt_version: str | None = None,
        omitted_source_ids: tuple[str, ...] = (),
    ) -> Self:
        source_ids = {source.source_id for source in context.sources}
        if any(set(scenario.source_ids) - source_ids for scenario in draft.scenarios):
            raise ValueError("scenario references an unknown source")
        return cls(
            **draft.model_dump(),
            schema_version="1",
            run_id=context.run_id,
            snapshot_id=context.snapshot.snapshot_id,
            expectation_origin="inferred" if context.mode == "lazy" else "requirements",
            prompt_version=prompt_version,
            omitted_source_ids=omitted_source_ids,
        )
