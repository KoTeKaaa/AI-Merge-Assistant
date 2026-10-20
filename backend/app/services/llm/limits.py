"""Per-stage allowance supplied by the orchestrator; never shared mutable run state."""

from typing import Annotated

from pydantic import Field

from .contracts import Contract


class StageBudget(Contract):
    remaining_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
    remaining_api_calls: Annotated[int, Field(ge=0, strict=True)]
    remaining_generations: Annotated[int, Field(ge=0, strict=True)]
    iteration: Annotated[int, Field(ge=0, strict=True)] = 0
