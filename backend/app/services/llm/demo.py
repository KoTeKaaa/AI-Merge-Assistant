"""Explicit offline or live artifact CLI; never imports or executes generated code."""

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .bundles import TestBundle
from .contracts import Contract, GenerationContext
from .fake import FakeProvider
from .feedback import ValidationFeedback
from .provider import ProviderError, ProviderResponse
from .scenarios import ScenarioSet
from .service import LLMService
from .settings import ConfigurationError, LLMSettings

MAX_INPUT_BYTES = 2_000_000


def _load[T: Contract](path: Path, schema: type[T]) -> T:
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    return schema.model_validate_json(raw, strict=True)


def _offline() -> tuple[GenerationContext, FakeProvider]:
    context = GenerationContext.model_validate(
        {
            "schema_version": "1",
            "run_id": "offline-example",
            "snapshot": {
                "kind": "upload",
                "snapshot_id": "offline-snapshot",
                "content_hash": "synthetic-double-v1",
            },
            "mode": "default",
            "sources": [
                {
                    "source_id": "spec",
                    "kind": "requirements",
                    "text": "double(value) returns value * 2 for every integer.",
                },
                {
                    "source_id": "code",
                    "kind": "code",
                    "path": "sample.py",
                    "text": "def double(value):\n    return value * 2\n",
                },
            ],
            "environment": {
                "profile_id": "python-pytest",
                "python_version": "3.13",
                "dependencies": ["pytest"],
            },
            "generated_tests_dir": "generated_tests",
            "protected_paths": ["sample.py"],
        }
    )
    scenarios = {
        "scenarios": [
            {
                "scenario_id": "double-integers",
                "given": ["An integer value"],
                "when": ["double(value) is called"],
                "then": ["The result equals value * 2"],
                "source_ids": ["spec", "code"],
            }
        ]
    }
    tests = {
        "files": [
            {
                "path": "generated_tests/test_sample.py",
                "scenario_ids": ["double-integers"],
                "content": (
                    "from sample import double\n\n"
                    "def test_double():\n    assert double(3) == 6\n"
                    "    assert double(0) == 0\n    assert double(-2) == -4\n"
                ),
            }
        ]
    }
    provider = FakeProvider(
        responses=[
            ProviderResponse(text=json.dumps(data), finish_reason="STOP")
            for data in (scenarios, tests)
        ],
        token_counts=[1, 1],
    )
    return context, provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate unverified LLM artifacts as JSON")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="Use the built-in scripted fake")
    mode.add_argument("--live", action="store_true", help="Explicitly call Gemini")
    parser.add_argument("--input", type=Path, help="GenerationContext JSON for live mode")
    parser.add_argument("--scenarios", type=Path, help="Previous ScenarioSet JSON for repair")
    parser.add_argument("--bundle", type=Path, help="Previous TestBundle JSON for repair")
    parser.add_argument("--feedback", type=Path, help="Validated external feedback JSON for repair")
    args = parser.parse_args(argv)
    repair_paths = (args.scenarios, args.bundle, args.feedback)
    if args.offline and (args.input or any(repair_paths)):
        parser.error("--offline uses only the built-in synthetic example")
    if args.live and args.input is None:
        parser.error("--live requires --input")
    if any(repair_paths) and not all(repair_paths):
        parser.error("repair requires --scenarios, --bundle and --feedback together")
    try:
        if args.offline:
            settings = LLMSettings(backend="fake", model_id="offline-scripted")
            context, provider = _offline()
        else:
            # Read only injected environment, never load .env or print configuration values.
            settings = LLMSettings.from_environment({**os.environ, "LLM_BACKEND": "gemini"})
            context = _load(args.input, GenerationContext)
            if all(repair_paths):
                scenarios = _load(args.scenarios, ScenarioSet)
                bundle = _load(args.bundle, TestBundle)
                feedback = _load(args.feedback, ValidationFeedback)
            from .gemini import GeminiProvider

            provider = GeminiProvider(settings)
        service = LLMService(provider, settings)
        output = {"backend": settings.backend, "validation_status": "unverified"}
        if all(repair_paths):
            output["repair"] = service.repair_tests(
                context, scenarios, bundle, feedback
            ).model_dump(mode="json")
        else:
            scenarios = service.generate_scenarios(context)
            # Emit completed stages even if the following stage fails.
            output["scenarios"] = scenarios.model_dump(mode="json")
            try:
                output["bundle"] = service.generate_tests(context, scenarios).model_dump(
                    mode="json"
                )
            except ProviderError as error:
                output["error"] = error.code
                output["metadata"] = error.metadata.model_dump(mode="json")
                print(json.dumps(output, ensure_ascii=False))
                return 1
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except ProviderError as error:
        output = {"error": error.code}
        if error.metadata is not None:
            output["metadata"] = error.metadata.model_dump(mode="json")
        print(json.dumps(output), file=sys.stderr)
    except ConfigurationError:
        print('{"error":"invalid_configuration"}', file=sys.stderr)
    except (OSError, ValueError, ValidationError, RecursionError):
        print('{"error":"invalid_input"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
