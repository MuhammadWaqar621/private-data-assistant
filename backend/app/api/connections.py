"""
Database-connection endpoints - registering, testing, re-indexing and
deleting the external databases a user wants to ask questions about.

This is one of the two places (with app/api/messages.py) that touch both
the DB/auth stack and app/engine/* - it checks auth/ownership, encrypts or
decrypts credentials, calls plain engine functions, and persists the
result. Nothing here ever returns a password in any form: `ConnectionOut`
has no field for it, encrypted or otherwise, so there is nothing to leak
even by accident.

Registration runs SYNCHRONOUSLY inside the request, exactly like the
sibling project's document ingestion and for the same reason (fewest
moving parts for this scope):

    row created (status=pending)
      -> status=indexing
      -> adapter.test_connection()      (fail -> status=failed + message)
      -> adapter.introspect_schema()    (fail -> status=failed + message)
      -> schema_rag.index_connection_schema()
      -> status=ready + schema_indexed_at

Every failure lands as `status=failed` with a human-readable
`error_message` on the returned row; the request itself never crashes and
never returns a 500. A production deployment would push this onto a
background worker and let the client poll - see README.md's "Roadmap /
known tradeoffs".

Two POST routes instead of one (a deliberate, documented deviation): the
network engines take a JSON body, while SQLite takes a multipart file
upload - one FastAPI route cannot describe both cleanly without giving up
request validation and the OpenAPI schema, so `POST /api/connections`
handles postgres/mysql/mssql/mongodb and `POST /api/connections/sqlite`
handles the file upload.
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.crypto import (
    DecryptionError,
    EncryptionNotConfiguredError,
    encrypt_secret,
    encryption_configured,
    decrypt_secret,
)
from app.db.session import get_db
from app.engine import blob_storage, example_rag, schema_rag
from app.engine.azure_client import ai_configured
from app.engine.db_adapters import get_adapter
from app.engine.db_adapters.base import ConnectionInfo
from app.engine.llm_provider import get_llm_provider_name
from app.models import ConnectionStatus, DatabaseConnection, DatabaseEngine, User

router = APIRouter(prefix="/api/connections", tags=["connections"])

SQLITE_FILENAME = "database.sqlite"

# Engines registered with a JSON body (everything except sqlite, which is
# a file upload).
_NETWORK_ENGINES = {
    DatabaseEngine.postgres,
    DatabaseEngine.mysql,
    DatabaseEngine.mssql,
    DatabaseEngine.mongodb,
}

_DEFAULT_PORTS = {
    DatabaseEngine.postgres: 5432,
    DatabaseEngine.mysql: 3306,
    DatabaseEngine.mssql: 1433,
    DatabaseEngine.mongodb: 27017,
}


# --- Schemas -----------------------------------------------------------------


class ConnectionCreate(BaseModel):
    """Body for a network-database registration.

    Note what is NOT here: anything identifying a user. The owner is always
    the authenticated caller."""

    name: str = Field(min_length=1, max_length=200)
    engine: DatabaseEngine
    host: Optional[str] = None
    port: Optional[int] = None
    database_name: str = Field(min_length=1, max_length=200)
    username: Optional[str] = None
    password: Optional[str] = None
    extra_params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "database_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("engine")
    @classmethod
    def _not_sqlite(cls, value: DatabaseEngine) -> DatabaseEngine:
        if value == DatabaseEngine.sqlite:
            raise ValueError(
                "SQLite databases are registered by uploading the file to "
                "POST /api/connections/sqlite, not through this endpoint."
            )
        return value


class ConnectionOut(BaseModel):
    """The ONLY shape a connection is ever serialized as.

    There is deliberately no password field of any kind - not the
    ciphertext, not a redacted placeholder, not a `has_password` boolean
    that could later tempt someone into adding the value next to it. The
    credential exists in exactly two places: the encrypted column, and a
    local variable for the microseconds before a driver connects."""

    id: int
    name: str
    engine: DatabaseEngine
    host: Optional[str]
    port: Optional[int]
    database_name: str
    username: Optional[str]
    extra_params: Dict[str, Any]
    status: ConnectionStatus
    error_message: Optional[str]
    schema_indexed_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class TestResult(BaseModel):
    ok: bool
    error: Optional[str] = None


# --- Helpers -------------------------------------------------------------


def _service_unavailable(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": error, "message": message},
    )


def _require_ai_configured() -> None:
    if ai_configured():
        return
    provider = get_llm_provider_name()
    chat_hint = "LLM_ENDPOINT*" if provider == "azure" else "GROQ_API_KEY"
    raise _service_unavailable(
        "ai_not_configured",
        "AI is not configured. Set AZURE_EM_* (embeddings, required to index "
        f"a database schema) and {chat_hint} (chat, provider={provider}) in .env.",
    )


def _require_encryption_configured() -> None:
    if encryption_configured():
        return
    raise _service_unavailable(
        "encryption_not_configured",
        "ENCRYPTION_KEY is not set, so database credentials cannot be stored "
        "securely. Generate one with: python -c \"from cryptography.fernet "
        'import Fernet; print(Fernet.generate_key().decode())" and set it in .env.',
    )


def _get_owned_connection(db: Session, connection_id: int, user: User) -> DatabaseConnection:
    connection = db.get(DatabaseConnection, connection_id)
    if connection is None or connection.user_id != user.id:
        # Deliberately identical to the "doesn't exist" case - a connection
        # belonging to another user must not be distinguishable from one
        # that was never created.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )
    return connection


def build_connection_info(connection: DatabaseConnection) -> ConnectionInfo:
    """Turn a stored row into the plain ConnectionInfo the engine takes,
    decrypting the password at the last possible moment.

    Called from here and from app/api/messages.py - the only two modules
    allowed to bridge the DB/crypto stack and app/engine/. The returned
    object is short-lived and never persisted, logged, or serialized."""
    password: Optional[str] = None
    if connection.encrypted_password:
        password = decrypt_secret(connection.encrypted_password)

    return ConnectionInfo(
        engine=connection.engine.value,
        database=connection.database_name,
        host=connection.host,
        port=connection.port,
        username=connection.username,
        password=password,
        extra_params=dict(connection.extra_params or {}),
    )


def _fail(db: Session, connection: DatabaseConnection, message: str) -> DatabaseConnection:
    connection.status = ConnectionStatus.failed
    connection.error_message = message
    db.commit()
    db.refresh(connection)
    return connection


def provision_connection(db: Session, connection: DatabaseConnection) -> DatabaseConnection:
    """test -> introspect -> embed -> ready/failed, synchronously.

    Never raises: every failure mode (bad credentials, unreachable host,
    an undecryptable password after a key rotation, an embedding or Postgres/pgvector
    outage) is recorded on the row as `status=failed` with a message the
    user can act on."""
    connection.status = ConnectionStatus.indexing
    connection.error_message = None
    db.commit()

    try:
        info = build_connection_info(connection)
    except DecryptionError as exc:
        return _fail(db, connection, str(exc))
    except EncryptionNotConfiguredError as exc:
        return _fail(db, connection, str(exc))

    try:
        adapter = get_adapter(connection.engine.value)
    except Exception as exc:  # noqa: BLE001
        return _fail(db, connection, str(exc))

    ok, error = adapter.test_connection(info)
    if not ok:
        return _fail(db, connection, error or "Could not connect to the database.")

    try:
        tables = adapter.introspect_schema(info)
    except Exception as exc:  # noqa: BLE001
        return _fail(db, connection, f"Could not read the database schema: {exc}")

    if not tables:
        return _fail(
            db,
            connection,
            "Connected successfully, but the database has no tables or "
            "collections this account can see - there is nothing to index.",
        )

    try:
        # Always clear the previous index first: a table that has since
        # been dropped must not linger in the schema index and get written
        # into a query.
        schema_rag.delete_connection_schema(connection.user_id, connection.id)
        schema_rag.index_connection_schema(
            user_id=connection.user_id,
            connection_id=connection.id,
            engine_name=connection.engine.value,
            tables=tables,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(db, connection, f"Could not index the database schema: {exc}")

    # Few-shot example queries (app/engine/example_rag.py): one per
    # foreign-key relationship, derived deterministically from the schema
    # just indexed above. Best-effort and non-fatal on purpose - unlike
    # schema indexing, a connection is still fully usable with zero
    # examples (the model just has one less nudge on its first question),
    # so a storage hiccup here must never flip a `ready` connection to
    # `failed`. Cleared first for the same re-index reason as the schema.
    try:
        example_rag.delete_connection_examples(connection.user_id, connection.id)
        example_rag.seed_fk_join_examples(
            user_id=connection.user_id,
            connection_id=connection.id,
            engine_name=connection.engine.value,
            tables=tables,
        )
    except Exception:  # noqa: BLE001 - optional polish, never blocks registration
        pass

    connection.status = ConnectionStatus.ready
    connection.error_message = None
    connection.schema_indexed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(connection)
    return connection


def _reject_if_duplicate(
    db: Session,
    user: User,
    engine: DatabaseEngine,
    host: Optional[str],
    port: Optional[int],
    database_name: str,
) -> None:
    """A user cannot register the same database twice - identified by
    (engine, host, port, database_name) for network engines, or
    (engine, database_name) for SQLite, which has no host/port at all.
    Host is a DNS name/IP, so compared case-insensitively; database_name
    is compared exactly, since database/collection names are routinely
    case-sensitive.

    This is deliberately narrower than just "same name": two different
    servers happening to both have a database called `analytics` are NOT
    the same database and must both be registerable. It is deliberately
    NOT scoped to username/password - the same physical database
    registered under two different credentials is still the same
    database, and letting it in twice would just mean asking the schema index to
    index (and the model to search) the identical schema under two
    different connection_ids for no benefit."""
    existing = (
        db.query(DatabaseConnection)
        .filter(
            DatabaseConnection.user_id == user.id,
            DatabaseConnection.engine == engine,
            DatabaseConnection.database_name == database_name,
        )
        .all()
    )
    normalized_host = (host or "").strip().lower() or None
    for connection in existing:
        if (connection.host or "").strip().lower() == normalized_host and connection.port == port:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"You've already registered this {engine.value} database "
                    f"({connection.name!r}) - use that connection instead of "
                    "adding a duplicate, or delete it first if you want to "
                    "re-register it."
                ),
            )


def _sqlite_blob_pathname(user_id: int, connection_id: int) -> str:
    """The server-derived Vercel Blob pathname for one uploaded database.

    Built from the AUTHENTICATED user id and the row's own id - never from
    the uploaded filename or any other client input, so there is no path
    traversal surface here at all. Mirrors the local-filesystem convention
    this project used before moving to Blob storage:
    {user_id}/{connection_id}/database.sqlite."""
    return f"{user_id}/{connection_id}/{SQLITE_FILENAME}"


# --- Endpoints -----------------------------------------------------------------


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(
    body: ConnectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatabaseConnection:
    """Register a network database (postgres / mysql / mssql / mongodb),
    then test + introspect + index its schema synchronously."""
    _require_encryption_configured()
    _require_ai_configured()

    if body.engine in _NETWORK_ENGINES and not (body.host or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A host is required for a {body.engine.value} connection.",
        )

    resolved_port = body.port or _DEFAULT_PORTS.get(body.engine)
    _reject_if_duplicate(
        db, current_user, body.engine, body.host, resolved_port, body.database_name
    )

    encrypted_password = encrypt_secret(body.password) if body.password else None

    connection = DatabaseConnection(
        user_id=current_user.id,
        name=body.name,
        engine=body.engine,
        host=(body.host or "").strip() or None,
        port=body.port or _DEFAULT_PORTS.get(body.engine),
        database_name=body.database_name,
        username=(body.username or "").strip() or None,
        encrypted_password=encrypted_password,
        extra_params=dict(body.extra_params or {}),
        status=ConnectionStatus.pending,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)

    return provision_connection(db, connection)


@router.post("/sqlite", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_sqlite_connection(
    file: UploadFile = File(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatabaseConnection:
    """Register a SQLite database by uploading its file.

    The file is uploaded to Vercel Blob at
    {user_id}/{connection_id}/database.sqlite (see app/engine/blob_storage.py)
    and the blob's public URL is recorded in
    `extra_params["storage_url"]`. There is no host, port, username or
    password - which is why the JSON endpoint above rejects `engine=sqlite`
    and points here."""
    _require_encryption_configured()
    _require_ai_configured()

    label = (name or "").strip()
    if not label:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A name is required.",
        )

    sqlite_database_name = file.filename or SQLITE_FILENAME
    _reject_if_duplicate(
        db, current_user, DatabaseEngine.sqlite, None, None, sqlite_database_name
    )

    connection = DatabaseConnection(
        user_id=current_user.id,
        name=label,
        engine=DatabaseEngine.sqlite,
        database_name=sqlite_database_name,
        extra_params={},
        status=ConnectionStatus.pending,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)

    # `file.file` is the underlying SpooledTemporaryFile - streamed to a
    # local temp file first (rather than read fully into memory), then
    # uploaded to Vercel Blob from that path and immediately removed. This
    # keeps this endpoint a plain `def` that FastAPI runs in its
    # threadpool, same as the blocking introspection below needs anyway.
    pathname = _sqlite_blob_pathname(current_user.id, connection.id)
    fd, temp_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        with open(temp_path, "wb") as handle:
            shutil.copyfileobj(file.file, handle)
        storage_url = blob_storage.upload_file(
            pathname, temp_path, content_type="application/x-sqlite3"
        )
    except (OSError, blob_storage.BlobStorageError) as exc:
        return _fail(db, connection, f"Could not save the uploaded database file: {exc}")
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    # Reassign (rather than mutate) so SQLAlchemy sees the JSON column change.
    connection.extra_params = {"storage_url": storage_url}
    db.commit()

    return provision_connection(db, connection)


@router.get("", response_model=List[ConnectionOut])
def list_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[DatabaseConnection]:
    return (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.user_id == current_user.id)
        .order_by(DatabaseConnection.created_at.desc(), DatabaseConnection.id.desc())
        .all()
    )


@router.get("/{connection_id}", response_model=ConnectionOut)
def get_connection(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatabaseConnection:
    return _get_owned_connection(db, connection_id, current_user)


@router.post("/{connection_id}/test", response_model=TestResult)
def test_connection(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TestResult:
    """Re-run just the connectivity check, without touching the schema
    index - what a "Test connection" button in the UI calls. Deliberately
    does NOT change `status`: a connection that is `ready` stays `ready`
    even if the database happens to be down right now."""
    connection = _get_owned_connection(db, connection_id, current_user)

    try:
        info = build_connection_info(connection)
    except (DecryptionError, EncryptionNotConfiguredError) as exc:
        return TestResult(ok=False, error=str(exc))

    try:
        adapter = get_adapter(connection.engine.value)
    except Exception as exc:  # noqa: BLE001
        return TestResult(ok=False, error=str(exc))

    ok, error = adapter.test_connection(info)
    return TestResult(ok=ok, error=error)


@router.post("/{connection_id}/reindex", response_model=ConnectionOut)
def reindex_connection(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatabaseConnection:
    """Re-introspect the database and rebuild its schema index from
    scratch - run this after the user changes their schema. The old
    schema_chunks/example_chunks rows are deleted first (inside provision_connection) so a dropped
    table disappears from the index rather than lingering."""
    _require_ai_configured()
    connection = _get_owned_connection(db, connection_id, current_user)
    return provision_connection(db, connection)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete the row, its schema/example rows, and (for SQLite) the
    uploaded database file's blob. Chats that referenced it keep their
    history but have `connection_id` set to NULL by the FK's ON DELETE SET
    NULL."""
    connection = _get_owned_connection(db, connection_id, current_user)

    try:
        schema_rag.delete_connection_schema(connection.user_id, connection.id)
    except Exception:  # noqa: BLE001 - a storage hiccup shouldn't block the delete
        pass
    try:
        example_rag.delete_connection_examples(connection.user_id, connection.id)
    except Exception:  # noqa: BLE001
        pass

    if connection.engine == DatabaseEngine.sqlite:
        storage_url = (connection.extra_params or {}).get("storage_url")
        if storage_url:
            try:
                blob_storage.delete_blob(storage_url)
            except blob_storage.BlobStorageError:  # noqa: BLE001 - never blocks the delete
                pass

    db.delete(connection)
    db.commit()
