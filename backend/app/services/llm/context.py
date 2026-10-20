"""Deterministic request construction from already prepared, untrusted source data."""

import json
from copy import deepcopy
from typing import Any

from .contracts import GenerationContext
from .provider import ProviderRequest
from .settings import ConfigurationError, LLMSettings


def build_request(
    context: GenerationContext,
    settings: LLMSettings,
    *,
    system_instruction: str,
    response_schema: dict[str, Any],
) -> ProviderRequest:
    """Serialize source text as JSON data; never splice it into system instructions."""
    if settings.model_id is None:
        raise ConfigurationError("Request construction requires an explicit model ID")
    payload = context.model_dump(mode="json")
    payload["sources"].sort(key=lambda source: source["source_id"])
    return ProviderRequest(
        model_id=settings.model_id,
        system_instruction=system_instruction,
        content=json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        response_schema=deepcopy(response_schema),
        max_output_tokens=settings.max_output_tokens,
        timeout_seconds=settings.request_timeout_seconds,
    )
