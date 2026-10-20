"""Provider port: SDK types and verified generation DTOs do not cross this boundary."""

from math import isfinite
from typing import Annotated, Any, Protocol

from pydantic import Field

from .contracts import Contract, Text

Counter = Annotated[int, Field(ge=0)]


class ProviderRequest(Contract):
    model_id: Text
    system_instruction: Annotated[str, Field(min_length=1, repr=False)]
    content: Annotated[str, Field(min_length=1, repr=False)]
    response_schema: Annotated[dict[str, Any], Field(min_length=1, repr=False)]
    max_output_tokens: Annotated[int, Field(gt=0)]
    timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class TokenUsage(Contract):
    input_tokens: Counter | None = None
    output_tokens: Counter | None = None
    total_tokens: Counter | None = None
    cached_tokens: Counter | None = None
    thought_tokens: Counter | None = None


class ProviderResponse(Contract):
    """Raw provider payload; parsing and validation happen in a later layer."""

    text: str | None = Field(default=None, repr=False)
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class ProviderError(RuntimeError):
    """Safe code and retry policy, never an original SDK diagnostic."""

    def __init__(
        self, code: str, *, retryable: bool = False, retry_after_seconds: float | None = None
    ) -> None:
        # Callers must supply an internal code, not text originating from the provider.
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = (
            retry_after_seconds
            if retry_after_seconds is not None
            and isfinite(retry_after_seconds)
            and retry_after_seconds >= 0
            else None
        )
        super().__init__(code)


class LLMProvider(Protocol):
    def generate(self, request: ProviderRequest) -> ProviderResponse: ...

    def count_tokens(self, request: ProviderRequest) -> int: ...
