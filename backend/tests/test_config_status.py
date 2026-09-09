"""
Tests for GET /api/config/status - the provider-aware `connections_llm`
group, the `encryption` group, `smtp`, and the `llm_provider` field.

These monkeypatch real env vars and clear the Settings cache rather than
patching the config_status module's own functions: the whole point is
proving the env-var-driven logic itself, not that some function got called.
"""

from cryptography.fernet import Fernet

from app.core.config import get_settings

_AZURE_EM_VARS = {
    "AZURE_EM_ENDPOINT": "https://example.openai.azure.com",
    "AZURE_EM_API_KEY": "fake-key",
    "AZURE_EM_API_VERSION": "2024-08-01-preview",
    "AZURE_EM_MODEL": "text-embedding-3-small",
}
_AZURE_CHAT_VARS = {
    "LLM_ENDPOINT": "https://example.openai.azure.com",
    "LLM_ENDPOINT_APIKEY": "fake-key",
    "LLM_MODEL_NAME": "gpt-4o-mini",
}


def _set(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


# --- connections_llm: embeddings AND the selected chat provider -------------


def test_false_when_embeddings_missing_even_if_groq_key_set(client, monkeypatch):
    monkeypatch.delenv("AZURE_EM_ENDPOINT", raising=False)
    _set(monkeypatch, LLM_PROVIDER="groq", GROQ_API_KEY="fake-groq-key")

    assert client.get("/api/config/status").json()["connections_llm"] is False


def test_true_with_embeddings_and_groq_key_when_provider_is_groq(client, monkeypatch):
    _set(monkeypatch, **_AZURE_EM_VARS, LLM_PROVIDER="groq", GROQ_API_KEY="fake-groq-key")
    monkeypatch.delenv("LLM_ENDPOINT", raising=False)  # azure chat vars deliberately unset

    body = client.get("/api/config/status").json()

    assert body["connections_llm"] is True
    assert body["llm_provider"] == "groq"


def test_false_when_provider_is_groq_but_groq_key_missing(client, monkeypatch):
    _set(monkeypatch, **_AZURE_EM_VARS, LLM_PROVIDER="groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert client.get("/api/config/status").json()["connections_llm"] is False


def test_true_with_embeddings_and_azure_chat_when_provider_is_azure(client, monkeypatch):
    _set(monkeypatch, **_AZURE_EM_VARS, **_AZURE_CHAT_VARS, LLM_PROVIDER="azure")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # groq key deliberately unset

    body = client.get("/api/config/status").json()

    assert body["connections_llm"] is True
    assert body["llm_provider"] == "azure"


def test_false_when_provider_is_azure_but_azure_chat_vars_missing(client, monkeypatch):
    _set(monkeypatch, **_AZURE_EM_VARS, LLM_PROVIDER="azure", GROQ_API_KEY="fake-groq-key")
    monkeypatch.delenv("LLM_ENDPOINT", raising=False)

    # Groq being configured must NOT count when the selected provider is
    # azure - only that provider's own vars matter.
    assert client.get("/api/config/status").json()["connections_llm"] is False


def test_embedding_dimensions_is_not_required(client, monkeypatch):
    """AZURE_EM_DIMENSIONS has a code default (1536), so a deployment that
    leaves it blank is still fully configured."""
    _set(monkeypatch, **_AZURE_EM_VARS, LLM_PROVIDER="groq", GROQ_API_KEY="k")
    monkeypatch.delenv("AZURE_EM_DIMENSIONS", raising=False)
    get_settings.cache_clear()

    assert client.get("/api/config/status").json()["connections_llm"] is True


def test_llm_provider_defaults_to_groq_when_unset(client, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    get_settings.cache_clear()

    assert client.get("/api/config/status").json()["llm_provider"] == "groq"


def test_llm_provider_falls_back_to_groq_for_an_unrecognized_value(client, monkeypatch):
    _set(monkeypatch, LLM_PROVIDER="anthropic")
    assert client.get("/api/config/status").json()["llm_provider"] == "groq"


# --- encryption ----------------------------------------------------------------


def test_encryption_is_true_for_a_valid_fernet_key(client, monkeypatch):
    _set(monkeypatch, ENCRYPTION_KEY=Fernet.generate_key().decode())
    assert client.get("/api/config/status").json()["encryption"] is True


def test_encryption_is_false_when_unset(client, monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    assert client.get("/api/config/status").json()["encryption"] is False


def test_encryption_is_false_for_a_malformed_key(client, monkeypatch):
    """A truthy-but-unusable key must report false HERE, so the failure
    surfaces as a clear 503 at save time rather than a 500 at encrypt
    time."""
    _set(monkeypatch, ENCRYPTION_KEY="not-a-real-fernet-key")
    assert client.get("/api/config/status").json()["encryption"] is False


# --- smtp -----------------------------------------------------------------------


def test_smtp_group_reflects_its_vars(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    get_settings.cache_clear()
    assert client.get("/api/config/status").json()["smtp"] is False

    _set(
        monkeypatch,
        SMTP_HOST="smtp.example.com",
        SMTP_USERNAME="user",
        SMTP_PASSWORD="pass",
        SMTP_FROM_EMAIL="noreply@example.com",
    )
    assert client.get("/api/config/status").json()["smtp"] is True


def test_config_status_is_public(client):
    assert client.get("/api/config/status").status_code == 200
