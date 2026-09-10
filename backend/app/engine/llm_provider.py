"""
Chat-completion provider selection.

Embeddings in this project are always Azure OpenAI (Groq has no
embeddings API, and schema-RAG needs embeddings) - only the CHAT half of
the pipeline is selectable, via the LLM_PROVIDER env var:
  - "groq"  (the default - used for any unset/blank/unrecognized value)
  - "azure"

This module is the single place that reads LLM_PROVIDER and hands back
the right (name, async client, model) tuple, so nothing else in the
engine has to re-implement that branching - app/engine/rag.py's
stream_agentic_reply() calls get_active_chat_provider() instead of
reaching into azure_client.py/groq_client.py directly, and
azure_client.ai_configured() calls chat_provider_configured() for the
combined "is the assistant ready" check used by app/api/connections.py and
app/api/messages.py.
"""

import os
from dataclasses import dataclass
from typing import Union

from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.engine import azure_client, groq_client


@dataclass(frozen=True)
class ActiveChatProvider:
    name: str  # "groq" | "azure"
    client: Union[AsyncOpenAI, AsyncAzureOpenAI]
    model: str


def get_llm_provider_name() -> str:
    """LLM_PROVIDER, normalized to "azure" or "groq" - "groq" is the
    default for an unset, blank, or unrecognized value."""
    value = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    return "azure" if value == "azure" else "groq"


def chat_provider_configured() -> bool:
    """True iff the currently-selected chat provider's own required env
    vars are fully set. Independent of embeddings config, which is always
    Azure and checked separately (azure_client.get_embedding_config())."""
    if get_llm_provider_name() == "azure":
        return azure_client.get_chat_config() is not None
    return groq_client.get_groq_chat_config() is not None


def get_active_chat_provider() -> ActiveChatProvider:
    """Return the (name, async client, model) for whichever chat provider
    is active. Raises RuntimeError if that provider isn't configured -
    callers must check chat_provider_configured() (or the combined
    azure_client.ai_configured(), which also requires embeddings) first,
    same contract as the rest of engine/."""
    if get_llm_provider_name() == "azure":
        config = azure_client.get_chat_config()
        if config is None:
            raise RuntimeError("Azure OpenAI chat is not configured (LLM_ENDPOINT* env vars).")
        return ActiveChatProvider(
            name="azure", client=azure_client.get_async_chat_client(), model=config.model
        )

    config = groq_client.get_groq_chat_config()
    if config is None:
        raise RuntimeError("Groq chat is not configured (GROQ_API_KEY env var).")
    return ActiveChatProvider(
        name="groq", client=groq_client.get_async_groq_chat_client(), model=config.model
    )


async def get_chat_model_chain(provider: ActiveChatProvider) -> list[str]:
    """Model ids to try, in order, for one chat-completion call.

    Groq-only: Groq enforces quota per model independently, so on a
    rate-limit error a different model can still succeed (see
    groq_client.GROQ_FALLBACK_MODELS for why these specific ids and not
    others). Azure OpenAI deployments don't have this per-model-swap
    option - a deployment is a fixed resource, not one of several
    interchangeable model ids - so Azure always returns just its own
    single configured model, unchanged from today's behavior."""
    if provider.name != "groq":
        return [provider.model]
    return await groq_client.live_groq_model_chain(provider.model, provider.client)
