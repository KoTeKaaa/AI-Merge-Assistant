"""Explicit scripted provider for offline use; never selected as a fallback."""

from collections import deque
from collections.abc import Iterable

from .provider import ProviderError, ProviderRequest, ProviderResponse


class FakeProvider:
    def __init__(
        self,
        responses: Iterable[ProviderResponse | Exception] = (),
        token_counts: Iterable[int | Exception] = (),
    ) -> None:
        self._responses = deque(responses)
        self._token_counts = deque(token_counts)
        self.generation_calls: list[ProviderRequest] = []
        self.token_count_calls: list[ProviderRequest] = []

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.generation_calls.append(request.model_copy(deep=True))
        if not self._responses:
            raise ProviderError("fake_responses_exhausted")
        result = self._responses.popleft()
        if isinstance(result, Exception):
            raise result
        if not isinstance(result, ProviderResponse):
            raise ProviderError("fake_invalid_response")
        return result

    def count_tokens(self, request: ProviderRequest) -> int:
        self.token_count_calls.append(request.model_copy(deep=True))
        if not self._token_counts:
            raise ProviderError("fake_token_counts_exhausted")
        result = self._token_counts.popleft()
        if isinstance(result, Exception):
            raise result
        if type(result) is not int or result < 0:
            raise ProviderError("fake_invalid_token_count")
        return result
