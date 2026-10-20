"""Injected LLM settings; no environment or .env is read on import."""

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class ConfigurationError(RuntimeError):
    """Configuration failure with a fixed, credential-free diagnostic."""


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    backend: Literal["gemini", "fake"] = "gemini"
    model_id: str | None = None
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    stage_timeout_seconds: float = Field(default=120, gt=0)
    request_timeout_seconds: float = Field(default=60, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    max_input_tokens: int = Field(default=24_000, gt=0)
    max_output_tokens: int = Field(default=8_000, gt=0)
    max_repairs: int = Field(default=2, ge=0)

    def __init__(self, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError:
            # Pydantic's structured errors may include original input, including secrets.
            raise ConfigurationError(
                "Invalid LLM settings: check credentials, model and limits"
            ) from None

    @model_validator(mode="after")
    def require_real_credentials(self) -> Self:
        if self.backend == "gemini" and (
            not self.model_id
            or not self.model_id.strip()
            or self.api_key is None
            or not self.api_key.get_secret_value().strip()
        ):
            raise ValueError("real provider requires credentials and model")
        if self.model_id is not None and self.model_id != self.model_id.strip():
            raise ValueError("model ID must not contain surrounding whitespace")
        return self

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> Self:
        """The application passes its environment explicitly; this method loads no files."""
        names = {
            "backend": "LLM_BACKEND",
            "model_id": "LLM_MODEL",
            "api_key": "GEMINI_API_KEY",
            "stage_timeout_seconds": "LLM_STAGE_TIMEOUT_SECONDS",
            "request_timeout_seconds": "LLM_REQUEST_TIMEOUT_SECONDS",
            "max_attempts": "LLM_MAX_ATTEMPTS",
            "max_input_tokens": "LLM_MAX_INPUT_TOKENS",
            "max_output_tokens": "LLM_MAX_OUTPUT_TOKENS",
            "max_repairs": "LLM_MAX_REPAIRS",
        }
        return cls(
            **{field: environment[name] for field, name in names.items() if name in environment}
        )
