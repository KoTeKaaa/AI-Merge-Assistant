"""Single-attempt Gemini Developer API adapter with per-call client ownership."""

from collections.abc import Callable
from math import ceil

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from .provider import ProviderError, ProviderRequest, ProviderResponse, TokenUsage
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
            if error.code in (401, 403):
                return ProviderError(
                    "provider_unauthorized" if error.code == 401 else "provider_forbidden"
                )
            if error.code == 404:
                return ProviderError("model_unavailable")
            if error.code == 429:
                return ProviderError("provider_quota", retryable=True)
            if error.code == 408:
                return ProviderError("provider_timeout", retryable=True)
            if error.code in (500, 502, 503, 504):
                return ProviderError("provider_unavailable", retryable=True)
            return ProviderError(
                "provider_invalid_request" if error.code == 400 else "provider_http_error"
            )
        if isinstance(error, httpx.TimeoutException):
            return ProviderError("provider_timeout", retryable=True)
        if isinstance(error, httpx.TransportError):
            return ProviderError("provider_unavailable", retryable=True)
        if isinstance(error, ValidationError):
            return ProviderError("invalid_provider_response")
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
            usage = self._usage(response.usage_metadata)
            feedback = response.prompt_feedback
            if feedback is not None and feedback.block_reason not in (
                None,
                types.BlockedReason.BLOCKED_REASON_UNSPECIFIED,
            ):
                return ProviderResponse(finish_reason="BLOCKED", usage=usage)
            candidates = response.candidates or []
            if not candidates:
                return ProviderResponse(usage=usage)
            if len(candidates) != 1:
                raise ProviderError("unexpected_candidates")
            candidate = candidates[0]
            parts = candidate.content.parts if candidate.content else []
            texts = []
            for part in parts or []:
                if set(part.model_dump(exclude_none=True)) - {
                    "text",
                    "thought",
                    "thought_signature",
                }:
                    raise ProviderError("unexpected_content")
                if part.thought:
                    continue
                if part.text is None:
                    raise ProviderError("unexpected_content")
                texts.append(part.text)
            return ProviderResponse(
                text="".join(texts) or None,
                finish_reason=candidate.finish_reason.value if candidate.finish_reason else None,
                usage=usage,
            )

        return self._execute(request, call)

    @staticmethod
    def _usage(metadata: types.GenerateContentResponseUsageMetadata | None) -> TokenUsage | None:
        if metadata is None:
            return None
        return TokenUsage(
            input_tokens=metadata.prompt_token_count,
            output_tokens=metadata.candidates_token_count,
            total_tokens=metadata.total_token_count,
            cached_tokens=metadata.cached_content_token_count,
            thought_tokens=metadata.thoughts_token_count,
        )

    def count_tokens(self, request: ProviderRequest) -> int:
        config = self._config(request)
        model = request.model_id
        generation_request = {
            "model": model if model.startswith("models/") else f"models/{model}",
            "contents": [{"role": "user", "parts": [{"text": request.content}]}],
            "systemInstruction": {"role": "user", "parts": [{"text": request.system_instruction}]},
            "tools": [],
            "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
            "generationConfig": {
                "candidateCount": 1,
                "maxOutputTokens": config.max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": request.response_schema,
            },
        }

        def call(client: genai.Client) -> int:
            self.api_calls += 1
            # SDK 2.29 lacks Developer API steering fields in CountTokensConfig.
            result = client.models.count_tokens(
                model=model,
                contents=None,
                config=types.CountTokensConfig(
                    http_options=types.HttpOptions(
                        extra_body={"generateContentRequest": generation_request}
                    )
                ),
            )
            if type(result.total_tokens) is not int or result.total_tokens < 0:
                raise ProviderError("invalid_token_count")
            return result.total_tokens

        return self._execute(request, call)
