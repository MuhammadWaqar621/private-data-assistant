"""
Shared pytest fixtures for the backend test suite.

Test database strategy
-----------------------
The integration tests (test_auth_api.py, test_chats_api.py,
test_connections_api.py, test_config_status.py) use FastAPI's TestClient
against an in-memory SQLite database, not the docker-compose Postgres
instance. This is a deliberate choice:

  - The SQLAlchemy models here (backend/app/models/) are simple - plain
    columns, standard `Enum`/`JSON`/`ForeignKey` types, and ORM-level
    relationship cascades rather than anything Postgres-specific (no JSONB,
    no array columns, no server-side triggers). SQLite supports everything
    the API layer actually exercises in these tests, `JSON` included
    (SQLAlchemy stores it as TEXT there).
  - `app/api/*.py` never writes raw SQL against this app's own database -
    every query goes through SQLAlchemy's Session/Query API.
  - SQLite-in-memory needs no running service, no migrations, and no
    teardown between test runs - each test gets a fresh schema
    (`Base.metadata.create_all` per test), which makes ownership/isolation
    assertions ("this row must not be visible to that user") trivial to
    reason about with no leftover state.

The tradeoff: this doesn't exercise Postgres-specific behavior (its `Enum`
type creates a real DB-level enum, and `chats.connection_id`'s ON DELETE
SET NULL is a real FK action that SQLite only enforces with
`PRAGMA foreign_keys=ON`). That's acceptable because none of these tests
depend on that distinction - they exercise API/ORM-level logic. The Alembic
migration is what actually runs against Postgres (see the README).

Two test files are deliberately different:

  - `test_pgvector_isolation.py` runs against a REAL Postgres+pgvector
    instance, with a disposable, uniquely-named Postgres schema per test
    (skipping cleanly, not failing, if none is reachable). pgvector's
    row-level filtering is the single most important property in this
    project, so it is tested against the real thing rather than a mock.
  - `test_db_adapters_readonly.py` imports nothing but
    `app/engine/db_adapters/readonly.py` (pure functions, stdlib only) and
    a real in-memory SQLite database, so the read-only guard is verified
    without needing MySQL/SQL Server/MongoDB servers anywhere.
"""

import os
import sys
import uuid
from pathlib import Path

# Make sure required env vars exist BEFORE any `app.*` module is imported -
# app/db/session.py reads Settings at import time to build its (unused in
# tests - see the `client` fixture below) module-level engine, and a
# missing JWT_SECRET_KEY would otherwise make every auth endpoint 503
# unless a test explicitly wants that (those tests unset it themselves via
# monkeypatch + get_settings.cache_clear()).
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-real-use")
os.environ.setdefault("DATABASE_URL", "sqlite:///./_unused_test_default.db")
# A real, valid Fernet key (generated once for the test suite - it protects
# nothing). app/core/crypto.py rejects a malformed key as "not configured",
# so this can't be a placeholder string.
os.environ.setdefault(
    "ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
)

# backend/ (the parent of this tests/ dir) needs to be on sys.path so
# `import app...` resolves the same way it does for alembic/uvicorn -
# mirrors alembic/env.py's own sys.path handling.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# The default above is a fixed string so imports are deterministic, but it
# must actually be a valid Fernet key - regenerate it here if it isn't,
# rather than failing every crypto-dependent test with a confusing error.
try:
    Fernet(os.environ["ENCRYPTION_KEY"].encode())
except Exception:  # noqa: BLE001
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import models  # noqa: E402,F401 - registers all models on Base.metadata
from app.core.config import get_settings  # noqa: E402
from app.db.base_class import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402

TEST_ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"]


@pytest.fixture()
def db_engine():
    """A fresh in-memory SQLite database per test, with every table
    created from the same Base.metadata Alembic manages for Postgres."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(db_engine, monkeypatch):
    """A TestClient wired to the per-test SQLite database via a
    dependency_overrides swap of get_db - the app's own module-level
    Postgres engine (app/db/session.py) is never touched."""
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # Valid defaults so most tests don't have to think about config - tests
    # that specifically exercise a "not configured" 503 unset these
    # themselves (see test_auth_api.py / test_connections_api.py).
    monkeypatch.setenv("JWT_SECRET_KEY", "test-only-secret-not-for-real-use")
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
    get_settings.cache_clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    get_settings.cache_clear()


# Satisfies app/core/security.py's validate_password_strength() (min
# length, upper/lower/digit/special) - used as the default test password
# everywhere so a policy change only needs updating in one place.
VALID_TEST_PASSWORD = "A-long-Password-123!"


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def signup(
    client: TestClient,
    email: str | None = None,
    password: str = VALID_TEST_PASSWORD,
    full_name: str = "Test User",
) -> dict:
    """Sign up a fresh user and return the token response body."""
    email = email or unique_email()
    response = client.post(
        "/api/auth/signup",
        json={"email": email, "full_name": full_name, "password": password},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    body["email"] = email
    body["password"] = password
    return body


def auth_headers(token_body: dict) -> dict:
    return {"Authorization": f"Bearer {token_body['access_token']}"}
