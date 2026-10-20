"""SDK-independent facade for bounded scenario and test generation stages."""

from collections.abc import Callable
from time import monotonic, sleep

from pydantic import ValidationError

from .budget import prepare_request
from .bundles import TestBundle, TestBundleDraft
from .contracts import GenerationContext
from .decoding import decode_scenarios, decode_tests
from .prompts import DEFAULT_SCENARIOS_PROMPT, LAZY_SCENARIOS_PROMPT, PYTEST_PROMPT
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

    @staticmethod
    def _validated_context(context: GenerationContext) -> GenerationContext:
        try:
            # Revalidate even DTOs copied with model_copy/model_construct at caller boundaries.
            return GenerationContext.model_validate(context.model_dump())
        except ValidationError:
            raise ProviderError("invalid_context") from None

    def generate_scenarios(self, context: GenerationContext) -> ScenarioSet:
        validated = self._validated_context(context)
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

    def generate_tests(self, context: GenerationContext, scenarios: ScenarioSet) -> TestBundle:
        validated = self._validated_context(context)
        try:
            validated_scenarios = ScenarioSet.model_validate(scenarios.model_dump())
            expected_origin = "inferred" if validated.mode == "lazy" else "requirements"
            if (
                validated_scenarios.run_id != validated.run_id
                or validated_scenarios.snapshot_id != validated.snapshot.snapshot_id
                or validated_scenarios.expectation_origin != expected_origin
            ):
                raise ValueError("scenarios do not match generation context")
            ScenarioSet.from_context(
                ScenarioDraftSet(
                    scenarios=validated_scenarios.scenarios,
                    ambiguities=validated_scenarios.ambiguities,
                ),
                validated,
            )
        except ValueError:
            raise ProviderError("invalid_scenarios") from None
        stage = StageProvider(self._provider, self._settings, clock=self._clock, wait=self._wait)
        prepared = prepare_request(
            validated,
            self._settings,
            stage,
            system_instruction=PYTEST_PROMPT.system_instruction,
            response_schema=TestBundleDraft.model_json_schema(),
            scenarios=validated_scenarios,
        )
        draft = decode_tests(stage.generate(prepared.request))
        try:
            return TestBundle.from_context(
                draft,
                prepared.context,
                validated_scenarios,
                prompt_version=PYTEST_PROMPT.version,
                omitted_source_ids=prepared.omitted_source_ids,
            )
        except ValueError:
            raise ProviderError("invalid_test_bundle") from None
