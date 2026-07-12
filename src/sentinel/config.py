"""Application configuration, loaded from the environment / a local .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Values are read from environment variables (case-insensitive) and, for local
    development, from a ``.env`` file at the project root.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime ---
    environment: str = "local"
    log_level: str = "INFO"
    json_logs: bool = True

    # --- LLM (Google Gemini) ---
    google_api_key: str | None = Field(default=None)
    gemini_model: str = "gemini-2.5-flash"
    llm_temperature: float = 0.1
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 2

    # --- Langfuse (optional) ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def llm_configured(self) -> bool:
        """True when a Gemini API key is available and the agent can run."""
        return bool(self.google_api_key)

    @property
    def langfuse_enabled(self) -> bool:
        """True when both Langfuse keys are present and tracing should be wired."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
