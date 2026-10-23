from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI-Merge-Assistant"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/ai_merge"
    redis_url: str = "redis://localhost:6379/0"

    # Ключ шифрования пользовательских токенов хранится отдельно от БД (раздел 6 team-guide.md)
    token_encryption_key: str = "dev-only-32-byte-secret-key-0000"
    session_secret_key: str = "dev-only-session-secret-key-0000"
    github_webhook_secret: str = "dev-only-webhook-secret"

    artifacts_volume_path: str = "/tmp/ai_merge_artifacts"


settings = Settings()
