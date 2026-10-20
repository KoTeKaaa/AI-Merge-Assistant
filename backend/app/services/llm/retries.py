"""One retry layer and one shared deadline for all provider calls in a stage."""

from collections.abc import Callable
from datetime import UTC
from email.utils import parsedate_to_datetime
from math import isfinite
from time import monotonic, sleep

from .limits import StageBudget
from .provider import LLMProvider, ProviderError, ProviderRequest, ProviderResponse
from .settings import ConfigurationError, LLMSettings


def parse_retry_after(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = max(0, date.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if isfinite(seconds) and seconds >= 0 else None


class StageProvider:
    """Wrap a single-attempt provider once per stage, including token counting."""

    def __init__(
        self,
        provider: LLMProvider,
        settings: LLMSettings,
        *,
        clock: Callable[[], float] = monotonic,
        wait: Callable[[float], None] = sleep,
        budget: StageBudget | None = None,
    ) -> None:
        if isinstance(provider, StageProvider):
            raise ConfigurationError("A stage retry layer cannot wrap another stage retry layer")
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._wait = wait
        self._deadline = clock() + min(
            settings.stage_timeout_seconds,
            budget.remaining_seconds if budget is not None else settings.stage_timeout_seconds,
        )
        self._max_calls = budget.remaining_api_calls if budget is not None else None
        self.api_calls = 0
        self.count_calls = 0
        self.generation_calls = 0
        self.generations = 0
        self.usage = None

    def _call[T](self, request: ProviderRequest, operation: Callable[[ProviderRequest], T]) -> T:
        for attempt in range(self._settings.max_attempts):
            remaining = self._deadline - self._clock()
            timeout = min(
                remaining, request.timeout_seconds, self._settings.request_timeout_seconds
            )
            if timeout < 0.001:
                raise ProviderError("deadline_exceeded")
            if self._max_calls is not None and self.api_calls >= self._max_calls:
                raise ProviderError("api_budget_exhausted")
            self.api_calls += 1
            if operation == self._provider.generate:
                self.generation_calls += 1
            else:
                self.count_calls += 1
            try:
                result = operation(request.model_copy(update={"timeout_seconds": timeout}))
            except ProviderError as error:
                if not error.retryable:
                    raise
                if self._clock() >= self._deadline:
                    raise ProviderError("deadline_exceeded") from None
                if attempt + 1 == self._settings.max_attempts:
                    raise ProviderError("attempts_exhausted") from None
                delay = max(2 ** min(attempt, 3), error.retry_after_seconds or 0)
                if delay >= self._deadline - self._clock():
                    raise ProviderError("deadline_exceeded") from None
                self._wait(delay)
            else:
                if self._clock() >= self._deadline:
                    raise ProviderError("deadline_exceeded")
                return result
        raise ProviderError("attempts_exhausted")

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.generations += 1
        response = self._call(request, self._provider.generate)
        self.usage = response.usage
        return response

    def count_tokens(self, request: ProviderRequest) -> int:
        return self._call(request, self._provider.count_tokens)
