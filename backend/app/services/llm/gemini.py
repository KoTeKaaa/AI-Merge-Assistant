"""Single-attempt Gemini Developer API adapter with per-call client ownership."""

from collections.abc import Callable
from math import ceil

import httpx
from google import genai
from google.genai import errors, types

from .provider import ProviderError, ProviderRequest, ProviderResponse
from .settings import ConfigurationError, LLMSettings


class GeminiProvider:
    def __init__(
        self, settings: LLMSettings, *, client_factory: Callable[..., genai.Client] = genai.Client
    ) -> None:
        if settings.backend != "gemini" or settings.api_key is None:
            raise ConfigurationError("Gemini provider requires real provider settings")
        self._settings = settings
        self._client_factory = client_factory
        self.api_calls = 0

    def _execute[T](self, request: ProviderRequest, operation: Callable[[genai.Client], T]) -> T:
        if request.model_id != self._settings.model_id:
            raise ProviderError("model_mismatch")
        timeout = min(request.timeout_seconds, self._settings.request_timeout_seconds)
        client = None
        failure = None
        try:
            client = self._client_factory(
                api_key=self._settings.api_key.get_secret_value(),
                vertexai=False,
                enterprise=False,
                http_options=types.HttpOptions(
                    base_url="https://generativelanguage.googleapis.com",
                    api_version="v1beta",
                    timeout=max(1, ceil(timeout * 1000)),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            result = operation(client)
        except Exception as error:
            failure = self._safe_error(error)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    if failure is None:
                        failure = ProviderError("client_close_failed")
        if failure is not None:
            raise failure from None
        return result

    @staticmethod
    def _safe_error(error: Exception) -> ProviderError:
        if isinstance(error, ProviderError):
            return error
        if isinstance(error, errors.APIError):
            return ProviderError("provider_http_error")
        if isinstance(error, httpx.TimeoutException):
            return ProviderError("provider_timeout", retryable=True)
        if isinstance(error, httpx.TransportError):
            return ProviderError("provider_unavailable", retryable=True)
        return ProviderError("provider_failure")

    def _config(self, request: ProviderRequest) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            response_mime_type="application/json",
            response_json_schema=request.response_schema,
            max_output_tokens=min(request.max_output_tokens, self._settings.max_output_tokens),
            candidate_count=1,
            tools=[],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="NONE")
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        def call(client: genai.Client) -> ProviderResponse:
            self.api_calls += 1
            response = client.models.generate_content(
                model=request.model_id, contents=request.content, config=self._config(request)
            )
            feedback = response.prompt_feedback
            if feedback is not None and feedback.block_reason not in (
                None,
                types.BlockedReason.BLOCKED_REASON_UNSPECIFIED,
            ):
                return ProviderResponse(finish_reason="BLOCKED")
            candidates = response.candidates or []
            if not candidates:
                return ProviderResponse()
            if len(candidates) != 1:
                raise ProviderError("unexpected_candidates")
            candidate = candidates[0]
            parts = candidate.content.parts if candidate.content else []
            texts = []
            for part in parts or []:
                if part.thought:
                    continue
                if part.text is None:
                    raise ProviderError("unexpected_content")
                texts.append(part.text)
            return ProviderResponse(
                text="".join(texts) or None,
                finish_reason=candidate.finish_reason.value if candidate.finish_reason else None,
            )

        return self._execute(request, call)
