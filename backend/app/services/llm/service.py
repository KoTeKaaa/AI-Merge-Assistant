"""SDK-independent facade for bounded scenario and test generation stages."""

from collections.abc import Callable
from time import monotonic, sleep

from pydantic import ValidationError

from .budget import prepare_request
from .bundles import TestBundle, TestBundleDraft
from .contracts import GenerationContext
from .decoding import decode_repair, decode_scenarios, decode_tests
from .feedback import ValidationFeedback
from .limits import StageBudget
from .prompts import (
    COVERAGE_REPAIR_PROMPT,
    DEFAULT_SCENARIOS_PROMPT,
    ERROR_REPAIR_PROMPT,
    LAZY_SCENARIOS_PROMPT,
    PYTEST_PROMPT,
)
from .provider import LLMProvider, ProviderError
from .repair import RepairDraft, RepairInput, RepairResult
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

    def _stage(self, budget: StageBudget | None, iteration: int) -> StageProvider:
        if type(iteration) is not int or iteration < 0:
            raise ProviderError("invalid_feedback")
        if budget is not None:
            try:
                budget = StageBudget.model_validate(budget.model_dump(warnings=False))
            except ValueError:
                raise ProviderError("invalid_budget") from None
            if budget.iteration != iteration:
                raise ProviderError("invalid_iteration")
            if budget.remaining_generations == 0:
                raise ProviderError("generation_budget_exhausted")
        if iteration > self._settings.max_repairs:
            raise ProviderError("repair_limit_exceeded")
        return StageProvider(
            self._provider, self._settings, clock=self._clock, wait=self._wait, budget=budget
        )

    def generate_scenarios(
        self, context: GenerationContext, *, budget: StageBudget | None = None
    ) -> ScenarioSet:
        return self._generate_scenarios(context, self._stage(budget, 0))

    def generate_tests(
        self,
        context: GenerationContext,
        scenarios: ScenarioSet,
        *,
        budget: StageBudget | None = None,
    ) -> TestBundle:
        return self._generate_tests(context, scenarios, self._stage(budget, 0))

    def repair_tests(
        self,
        context: GenerationContext,
        scenarios: ScenarioSet,
        previous_bundle: TestBundle,
        feedback: ValidationFeedback,
        *,
        budget: StageBudget | None = None,
    ) -> RepairResult:
        return self._repair_tests(
            context, scenarios, previous_bundle, feedback, self._stage(budget, feedback.iteration)
        )

    @staticmethod
    def _validated_context(context: GenerationContext) -> GenerationContext:
        try:
            # Revalidate even DTOs copied with model_copy/model_construct at caller boundaries.
            return GenerationContext.model_validate(context.model_dump(warnings=False))
        except ValidationError:
            raise ProviderError("invalid_context") from None

    def _generate_scenarios(self, context: GenerationContext, stage: StageProvider) -> ScenarioSet:
        validated = self._validated_context(context)
        template = (
            DEFAULT_SCENARIOS_PROMPT if validated.mode == "default" else LAZY_SCENARIOS_PROMPT
        )
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

    @staticmethod
    def _validated_scenarios(context: GenerationContext, scenarios: ScenarioSet) -> ScenarioSet:
        try:
            validated_scenarios = ScenarioSet.model_validate(scenarios.model_dump(warnings=False))
            expected_origin = "inferred" if context.mode == "lazy" else "requirements"
            if (
                validated_scenarios.run_id != context.run_id
                or validated_scenarios.snapshot_id != context.snapshot.snapshot_id
                or validated_scenarios.expectation_origin != expected_origin
            ):
                raise ValueError("scenarios do not match generation context")
            ScenarioSet.from_context(
                ScenarioDraftSet(
                    scenarios=validated_scenarios.scenarios,
                    ambiguities=validated_scenarios.ambiguities,
                ),
                context,
            )
            return validated_scenarios
        except ValueError:
            raise ProviderError("invalid_scenarios") from None

    def _generate_tests(
        self, context: GenerationContext, scenarios: ScenarioSet, stage: StageProvider
    ) -> TestBundle:
        validated = self._validated_context(context)
        validated_scenarios = self._validated_scenarios(validated, scenarios)
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

    def _repair_tests(
        self,
        context: GenerationContext,
        scenarios: ScenarioSet,
        previous_bundle: TestBundle,
        feedback: ValidationFeedback,
        stage: StageProvider,
    ) -> RepairResult:
        validated = self._validated_context(context)
        validated_scenarios = self._validated_scenarios(validated, scenarios)
        try:
            previous = TestBundle.model_validate(previous_bundle.model_dump(warnings=False))
            TestBundle.from_context(
                TestBundleDraft(files=previous.files), validated, validated_scenarios
            )
            checked = ValidationFeedback.model_validate(
                feedback.model_dump(warnings=False)
            ).validate_for(validated, previous)
        except ValueError:
            raise ProviderError("invalid_feedback") from None
        if checked.iteration > self._settings.max_repairs:
            raise ProviderError("repair_limit_exceeded")
        if checked.reason == "coverage_missing":
            raise ProviderError("coverage_report_missing")
        template = (
            COVERAGE_REPAIR_PROMPT if checked.reason == "coverage_gap" else ERROR_REPAIR_PROMPT
        )
        prepared = prepare_request(
            validated,
            self._settings,
            stage,
            system_instruction=template.system_instruction,
            response_schema=RepairDraft.model_json_schema(),
            scenarios=validated_scenarios,
            repair=RepairInput(previous_bundle=previous, feedback=checked),
        )
        decision = decode_repair(stage.generate(prepared.request))
        replacement = None
        if decision.bundle is not None:
            try:
                replacement = TestBundle.from_context(
                    decision.bundle,
                    prepared.context,
                    validated_scenarios,
                    prompt_version=template.version,
                    omitted_source_ids=prepared.omitted_source_ids,
                )
                old_links = {
                    (file.path, case) for file in previous.files for case in file.scenario_ids
                }
                new_links = {
                    (file.path, case) for file in replacement.files for case in file.scenario_ids
                }
                if not old_links <= new_links:
                    raise ValueError("repair must preserve previous files and scenario links")
            except ValueError:
                raise ProviderError("invalid_repair_bundle") from None
            if replacement.bundle_hash == previous.bundle_hash:
                raise ProviderError("repair_unchanged")
        return RepairResult(
            action=decision.action,
            bundle=replacement,
            explanation=decision.explanation,
            schema_version="1",
            run_id=validated.run_id,
            snapshot_id=validated.snapshot.snapshot_id,
            previous_bundle_hash=previous.bundle_hash,
            iteration=checked.iteration,
        )
