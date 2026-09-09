"""
Thin wrapper around the Groq API, used for chat completions when selected
via LLM_PROVIDER (see app/engine/llm_provider.py).

Configuration is read directly from environment variables (not from
app.core.config.Settings) so that app/engine/ has zero dependency on the
rest of the FastAPI application - see app/engine/__init__.py for the full
isolation contract and app/engine/azure_client.py's module docstring for
the same reasoning applied to Azure OpenAI.

Groq exposes an OpenAI-compatible API, so the `openai` package (already a
dependency, used for Azure OpenAI elsewhere in this engine) is reused here
too, just pointed at Groq's base URL - no separate SDK is needed. Note
that Groq has no embeddings API, which is why embeddings in this project
are always Azure regardless of LLM_PROVIDER.

Env vars:
  - GROQ_API_KEY:   required for anything in this module to work.
  - GROQ_LLM_MODEL: chat-completion model used when LLM_PROVIDER="groq"
                    (the default - see app/engine/llm_provider.py).

> Groq periodically retires model IDs. If a chat call fails with
> `model_not_found` / `model_decommissioned`, update GROQ_LLM_MODEL in
> .env to whatever Groq currently recommends - nothing in the code needs
> to change.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from openai import AsyncOpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

DEFAULT_CHAT_MODEL = "openai/gpt-oss-120b"


@dataclass(frozen=True)
class GroqChatConfig:
    model: str


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _api_key() -> Optional[str]:
    return _clean(os.getenv("GROQ_API_KEY"))


def groq_configured() -> bool:
    """True iff GROQ_API_KEY is set to a non-empty value."""
    return _api_key() is not None


def get_groq_chat_config() -> Optional[GroqChatConfig]:
    """GROQ_LLM_MODEL, defaulting to DEFAULT_CHAT_MODEL - None (not
    configured) when GROQ_API_KEY itself is unset, same shape as
    azure_client.get_chat_config()."""
    if not groq_configured():
        return None
    model = _clean(os.getenv("GROQ_LLM_MODEL")) or DEFAULT_CHAT_MODEL
    return GroqChatConfig(model=model)


@lru_cache
def get_async_groq_chat_client() -> AsyncOpenAI:
    """Async client - used for streaming chat completions when
    LLM_PROVIDER="groq" (see app/engine/llm_provider.py), mirroring
    azure_client.get_async_chat_client()."""
    api_key = _api_key()
    if api_key is None:
        raise RuntimeError("Groq is not configured (GROQ_API_KEY env var).")
    return AsyncOpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
