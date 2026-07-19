"""Embedding model factory (Gemini), mirroring the chat-model provider seam."""

from __future__ import annotations

from langchain_core.embeddings import Embeddings

from sentinel.config import Settings


def get_embeddings(settings: Settings) -> Embeddings:
    """Build the Google embedding model from settings.

    Raises:
        RuntimeError: if no Gemini API key is configured.
    """
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured; cannot build embeddings.")

    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    return GoogleGenerativeAIEmbeddings(
        model=settings.embedding_model,
        google_api_key=settings.google_api_key,
        # Truncate to embedding_dim so vectors fit pgvector's HNSW index limit.
        output_dimensionality=settings.embedding_dim,
    )
