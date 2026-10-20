"""Bounded token checks; only explicitly optional supporting sources may be removed."""

from typing import Annotated, Any

from pydantic import Field

from .context import build_request
from .contracts import Contract, GenerationContext, Text
from .provider import LLMProvider, ProviderError, ProviderRequest
from .scenarios import ScenarioSet
from .settings import LLMSettings


class BudgetedRequest(Contract):
    context: GenerationContext = Field(repr=False)
    request: ProviderRequest = Field(repr=False)
    input_tokens: Annotated[int, Field(ge=0)]
    omitted_source_ids: tuple[Text, ...] = ()
    count_checks: Annotated[int, Field(ge=1, le=2)]


def prepare_request(
    context: GenerationContext,
    settings: LLMSettings,
    provider: LLMProvider,
    *,
    system_instruction: str,
    response_schema: dict[str, Any],
    scenarios: ScenarioSet | None = None,
) -> BudgetedRequest:
    """Return a request within the input budget; never call generate here."""
    current = context
    pinned_ids = (
        {source_id for scenario in scenarios.scenarios for source_id in scenario.source_ids}
        if scenarios is not None
        else set()
    )
    omitted: tuple[str, ...] = ()
    for check in (1, 2):
        request = build_request(
            current,
            settings,
            system_instruction=system_instruction,
            response_schema=response_schema,
            scenarios=scenarios,
        )
        tokens = provider.count_tokens(request)
        if type(tokens) is not int or tokens < 0:
            raise ProviderError("invalid_token_count")
        if tokens <= settings.max_input_tokens:
            return BudgetedRequest(
                context=current,
                request=request,
                input_tokens=tokens,
                omitted_source_ids=omitted,
                count_checks=check,
            )
        omitted = tuple(
            sorted(
                source.source_id
                for source in context.sources
                if source.optional and source.source_id not in pinned_ids
            )
        )
        if check == 2 or not omitted:
            raise ProviderError("context_limit_exceeded")
        current = GenerationContext.model_validate(
            {
                **context.model_dump(),
                "sources": [
                    source.model_dump()
                    for source in context.sources
                    if source.source_id not in omitted
                ],
            }
        )
    raise ProviderError("context_limit_exceeded")
