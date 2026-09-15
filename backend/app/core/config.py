"""
Application configuration.

Settings are loaded from environment variables (and a local .env file when
present) via pydantic-settings. Nothing here should hold secrets directly -
values are supplied through the environment at runtime (see .env.example
at the repo root for the full list of variables).

Note the split, identical in spirit to this project's sibling
(private-document-assistant): most of the backend reads configuration
through this Settings object, but app/engine/ deliberately does NOT - it
reads os.environ directly so that package has zero dependency on the rest
of the app (see app/engine/__init__.py's isolation contract). Both halves
still read the same .env file.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application settings.

    Grouped roughly by concern. Auth-related settings (JWT, SMTP) back the
    endpoints in app/api/auth.py - each optional group is considered
    "configured" only once every variable in it is set (see
    app/api/config_status.py), and the endpoints that depend on an
    unconfigured group return a clear 503 rather than crashing.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App metadata -----------------------------------------------------
    APP_NAME: str = "Private Data Assistant"
    ENVIRONMENT: str = "development"

    # --- Database (this app's OWN metadata database) ----------------------
    # Holds users, chats, messages, the *registration records* for the
    # user's external databases, AND (via the `vector` extension) the
    # schema/example embeddings that used to live in a separate Qdrant
    # service - see app/engine/vector_store.py. Never any of the user's
    # actual business data, which is only ever read live, on demand, from
    # their own DB. On Vercel this is the Vercel Postgres (Neon-backed)
    # connection string.
    DATABASE_URL: str = "postgresql://postgres:postgres@postgres:5432/private_data_assistant"

    # --- Frontend (used to build links in emails) -------------------------
    FRONTEND_URL: str = "http://localhost:5173"

    # --- Credential encryption (Fernet) -----------------------------------
    # Required before any database connection can be registered: the
    # user's DB password is encrypted with this key before it is written to
    # `database_connections.encrypted_password`, and decrypted in memory
    # only, immediately before opening a connection to their DB. See
    # app/core/crypto.py. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ENCRYPTION_KEY: Optional[str] = None

    # --- Query execution limits (user's external DBs) ---------------------
    # Hard caps applied by app/api/messages.py when it executes a query the
    # model wrote - see app/engine/db_adapters/ for how each engine
    # enforces them.
    MAX_QUERY_ROWS: int = 200
    QUERY_TIMEOUT_SECONDS: int = 15

    # --- Azure OpenAI - Embeddings ----------------------------------------
    # Always required: schema-RAG (app/engine/schema_rag.py) embeds table
    # schemas and questions through Azure, regardless of LLM_PROVIDER.
    AZURE_EM_ENDPOINT: Optional[str] = None
    AZURE_EM_API_KEY: Optional[str] = None
    AZURE_EM_API_VERSION: Optional[str] = None
    AZURE_EM_MODEL: Optional[str] = None
    # Embedding vector size, used to size the pgvector columns (see
    # app/engine/vector_store.py). Not required for the `connections_llm`
    # config-status group - it has a sensible code default (1536) in
    # app/engine/azure_client.get_embedding_dimensions(). Declared here
    # (even though app/engine/ reads env vars directly, not this Settings
    # object) purely so it shows up alongside the other AZURE_EM_* vars for
    # anyone inspecting Settings.
    AZURE_EM_DIMENSIONS: str = "1536"

    # --- Azure OpenAI - Chat ----------------------------------------------
    LLM_ENDPOINT: Optional[str] = None
    LLM_ENDPOINT_APIKEY: Optional[str] = None
    LLM_MODEL_NAME: Optional[str] = None

    # --- Chat provider selection ------------------------------------------
    # "groq" (default) or "azure" - see app/engine/llm_provider.py. Only
    # the CHAT half of the pipeline is selectable; embeddings above are
    # always Azure OpenAI (Groq has no embeddings API).
    LLM_PROVIDER: str = "groq"

    # --- Groq (chat completions when LLM_PROVIDER=groq) -------------------
    GROQ_API_KEY: Optional[str] = None
    GROQ_LLM_MODEL: str = "openai/gpt-oss-120b"

    # --- JWT (auth) --------------------------------------------------------
    JWT_SECRET_KEY: Optional[str] = None
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # --- SMTP (forgot-password emails) -------------------------------------
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM_EMAIL: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (env is only parsed once)."""
    return Settings()
