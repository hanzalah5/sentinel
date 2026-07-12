"""LLM factory.

A thin seam over the chat model so the rest of the system depends on a
``BaseChatModel`` rather than a concrete provider. Today it returns Gemini; a later
iteration can return a cost/latency-aware router without touching the agent.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from sentinel.config import Settings


def get_chat_model(settings: Settings) -> BaseChatModel:
    """Build the Gemini chat model from settings.

    Raises:
        RuntimeError: if no Gemini API key is configured.
    """
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not configured; cannot build the Gemini chat model.")

    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        temperature=settings.llm_temperature,
        google_api_key=settings.google_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
