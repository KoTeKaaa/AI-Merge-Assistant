"""SDK-independent facade for a single bounded scenario generation stage."""

from collections.abc import Callable
from time import monotonic, sleep

from pydantic import ValidationError

from .budget import prepare_request
from .contracts import GenerationContext
from .decoding import decode_scenarios
from .prompts import DEFAULT_SCENARIOS_PROMPT, LAZY_SCENARIOS_PROMPT
from .provider import LLMProvider, ProviderError
from .retries import StageProvider
from .scenarios import ScenarioDraftSet, ScenarioSet
from .settings import LLMSettings


class LLMService:
    def __init__(
        self,
        provider: LLMProvider,
        settings: LLMSettings,
        *,
        clock: Callable[[], float] = monotonic,
        wait: Callable[[float], None] = sleep,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._wait = wait

    def generate_scenarios(self, context: GenerationContext) -> ScenarioSet:
        try:
            # Revalidate even DTOs copied with model_copy/model_construct at caller boundaries.
            validated = GenerationContext.model_validate(context.model_dump())
        except ValidationError:
            raise ProviderError("invalid_context") from None
        template = (
            DEFAULT_SCENARIOS_PROMPT if validated.mode == "default" else LAZY_SCENARIOS_PROMPT
        )
        stage = StageProvider(self._provider, self._settings, clock=self._clock, wait=self._wait)
        prepared = prepare_request(
            validated,
            self._settings,
            stage,
            system_instruction=template.system_instruction,
            response_schema=ScenarioDraftSet.model_json_schema(),
        )
        response = stage.generate(prepared.request)
        draft = decode_scenarios(response)
        try:
            return ScenarioSet.from_context(
                draft,
                prepared.context,
                prompt_version=template.version,
                omitted_source_ids=prepared.omitted_source_ids,
            )
        except ValueError:
            raise ProviderError("invalid_source_reference") from None
