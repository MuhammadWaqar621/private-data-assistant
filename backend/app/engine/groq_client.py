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
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from openai import AsyncOpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

DEFAULT_CHAT_MODEL = "openai/gpt-oss-120b"

# Groq enforces quota per model, independently - if the primary model's
# daily/per-minute limit is hit, a different model still has its own
# untouched budget (confirmed by direct testing on this same Groq
# account: querynest-website hit gpt-oss-120b's 200k-tokens/day cap while
# qwen3.8-27b kept succeeding the whole time). app/engine/rag.py tries
# these in order on a rate-limit error - see llm_provider.get_chat_model_chain().
# Groq-only: Azure OpenAI has its own quota and never needs a fallback
# chain, see llm_provider.py.
#
# This is our own VETTED order, not "whatever Groq happens to list" -
# each id here was evaluated for answer quality before being added
# (gpt-oss-20b hallucinated an unsupported detail in isolated testing on
# a sibling project's system prompt; qwen3.8-27b didn't). `groq/compound`
# is deliberately excluded - it runs on top of gpt-oss-120b internally
# and shares that model's quota rather than having its own.
GROQ_FALLBACK_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"]

# Cache of Groq's live /v1/models listing, so a fallback isn't attempted
# against a model Groq has quietly retired/renamed - checked dynamically
# rather than assumed, cached so this doesn't cost an extra API call on
# every single chat turn.
_MODEL_LIST_TTL_SECONDS = 3600
_model_list_cache: set[str] = set()
_model_list_cached_at: float = 0.0


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


async def live_groq_model_chain(primary: str, client: AsyncOpenAI) -> list[str]:
    """[primary, *GROQ_FALLBACK_MODELS], filtered down to whichever ids
    Groq actually still serves right now, so a retired/renamed model
    drops out of the chain automatically instead of wasting a request on
    every single chat turn. Cached for _MODEL_LIST_TTL_SECONDS so this
    only queries Groq's /v1/models occasionally.

    Takes `client` rather than fetching its own via
    get_async_groq_chat_client() - reuses whatever client the caller is
    already using (real in production, a fake/mock in tests), instead of
    a second, separately-authenticated client that unit tests couldn't
    intercept."""
    global _model_list_cache, _model_list_cached_at  # noqa: PLW0603 - simple process-local cache

    vetted = list(dict.fromkeys([primary, *GROQ_FALLBACK_MODELS]))  # de-dup, preserve order
    now = time.monotonic()
    if not _model_list_cache or (now - _model_list_cached_at) > _MODEL_LIST_TTL_SECONDS:
        try:
            live = await client.models.list()
            _model_list_cache = {m.id for m in live.data}
            _model_list_cached_at = now
        except Exception:  # noqa: BLE001 - listing failed; use whatever we had (or the full vetted list)
            if not _model_list_cache:
                return vetted

    chain = [m for m in vetted if m in _model_list_cache]
    # If the live listing looked empty/wrong somehow, don't strand the
    # caller with zero models to try.
    return chain or vetted
