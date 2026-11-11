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
    gemini_model: str = "gemini-3.8-flash"
    llm_temperature: float = 0.1
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 2

    # --- Langfuse (optional) ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- Runbook RAG (Postgres + pgvector) ---
    database_url: str | None = None
    # gemini-embedding-001 defaults to 3072 dims; we request 768 (Matryoshka
    # truncation) to stay under pgvector's 2000-dim HNSW index limit.
    embedding_model: str = "models/gemini-embedding-001"
    # Bounded: pgvector's HNSW index rejects columns with more than 2000 dimensions.
    embedding_dim: int = Field(default=768, ge=1, le=2000)
    runbook_top_k: int = 3
    db_connect_timeout_seconds: int = 5
    db_statement_timeout_ms: int = 5000
    db_pool_max_size: int = 5

    # --- Human-in-the-loop remediation ---
    # Actions at or below this risk auto-execute; anything riskier needs approval.
    # Default "none" => every remediation requires human approval (safest). Set to
    # "low"/"medium"/"high" to auto-approve up to that risk.
    hitl_auto_approve_max_risk: str = "none"

    # --- Ingestion (Redis Streams) ---
    redis_url: str = "redis://localhost:6379/0"
    alert_stream: str = "sentinel:alerts"
    consumer_group: str = "sentinel-workers"
    consumer_name: str = "worker-1"
    dead_letter_stream: str = "sentinel:alerts:dlq"
    max_delivery_attempts: int = 3
    stream_block_ms: int = 5000
    stream_batch_count: int = 10
    reclaim_idle_ms: int = 30000
    # Approximate cap on stream length so Redis memory stays bounded (0 = unbounded).
    stream_maxlen: int = 100_000

    @property
    def llm_configured(self) -> bool:
        """True when a Gemini API key is available and the agent can run."""
        return bool(self.google_api_key)

    @property
    def langfuse_enabled(self) -> bool:
        """True when both Langfuse keys are present and tracing should be wired."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def rag_enabled(self) -> bool:
        """True when a database is configured and runbook retrieval should be wired."""
        return bool(self.database_url)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
