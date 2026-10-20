"""Strict decoding of untrusted model text, before service-level context binding."""

import json
from typing import Any

from pydantic import ValidationError

from .bundles import TestBundleDraft
from .contracts import Contract
from .provider import ProviderError, ProviderResponse
from .repair import RepairDraft
from .scenarios import ScenarioDraftSet


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _decode[T: Contract](response: ProviderResponse, schema: type[T]) -> T:
    if response.finish_reason in {
        "BLOCKED",
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    }:
        raise ProviderError("provider_refusal")
    if response.finish_reason == "MAX_TOKENS":
        raise ProviderError("truncated_response")
    if response.text is None or not response.text.strip():
        raise ProviderError("empty_response")
    if response.finish_reason != "STOP":
        raise ProviderError("incomplete_response")
    try:
        # Pydantic accepts duplicate keys; reject them and nonstandard JSON explicitly.
        json.loads(response.text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ProviderError("invalid_json") from None
    try:
        # Strict JSON mode still accepts arrays for tuple fields, without scalar coercions.
        return schema.model_validate_json(response.text, strict=True)
    except ValidationError:
        raise ProviderError("invalid_schema") from None


def decode_scenarios(response: ProviderResponse) -> ScenarioDraftSet:
    return _decode(response, ScenarioDraftSet)


def decode_tests(response: ProviderResponse) -> TestBundleDraft:
    return _decode(response, TestBundleDraft)


def decode_repair(response: ProviderResponse) -> RepairDraft:
    return _decode(response, RepairDraft)
