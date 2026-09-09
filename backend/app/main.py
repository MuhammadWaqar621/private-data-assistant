"""
Private Data Assistant backend entrypoint.

The product: a user registers a connection to their OWN external database
(PostgreSQL, MySQL/MariaDB, SQL Server, SQLite or MongoDB) and asks
questions about their live data in plain language. The backend indexes that
database's SCHEMA into Qdrant, retrieves the relevant tables per question,
and lets an LLM write a read-only query which it then executes against the
real database - see README.md for the full flow.

Router layout mirrors the sibling project (private-document-assistant):
config-status, auth, chats, the domain resource (connections here,
documents there), and the streaming message endpoint. app/api/connections.py
and app/api/messages.py are the only modules that bridge the DB/auth stack
and the independent app/engine/ package (see app/engine/__init__.py for its
isolation contract and the generator handshake between rag.py and
messages.py).
"""

from dotenv import load_dotenv

# Must run before any app.engine module reads its env vars (they use
# os.getenv directly - see app/engine/__init__.py's isolation contract).
# Under docker-compose this is a no-op (env_file: .env already exports
# everything into the real process environment); it's required for the
# README's documented "Running the backend without Docker" path, where
# nothing else ever loads .env into os.environ - pydantic-settings'
# `Settings(env_file=".env")` parses the file for its own model only, it
# does not export those values into os.environ for other code to read.
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.chats import router as chats_router
from app.api.config_status import router as config_status_router
from app.api.connections import router as connections_router
from app.api.messages import router as messages_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="Private Data Assistant API",
    description=(
        "Ask questions about your own databases in plain language - "
        "schema-RAG + read-only text-to-query, backend API"
    ),
    version="0.1.0",
)

# Permissive CORS for local development. Tighten this once the frontend
# origin(s) are finalized for a real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(config_status_router)
app.include_router(auth_router)
app.include_router(chats_router)
app.include_router(connections_router)
app.include_router(messages_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Basic liveness check used by docker-compose / uptime probes."""
    return {"status": "ok", "app": settings.APP_NAME}
