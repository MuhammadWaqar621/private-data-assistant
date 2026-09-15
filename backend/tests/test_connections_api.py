"""
Integration tests for /api/connections/*.

No real external database is involved: `get_adapter` is monkeypatched to a
fake adapter and `schema_rag`'s indexing calls to no-ops, so these tests
target the endpoint's OWN logic - ownership, credential handling, and the
pending -> indexing -> ready/failed status machine - rather than driver
behavior (which test_db_adapters_readonly.py covers) or pgvector filtering
(test_pgvector_isolation.py). SQLite uploads similarly mock
app/engine/blob_storage.py's upload/delete functions (see the `fake_blob`
fixture) rather than talking to Vercel Blob for real.

The most important assertions in this file are the credential ones: a
password must go in, be stored encrypted, be decryptable server-side, and
NEVER appear in any response body in any form.
"""

from typing import Dict

import pytest

import app.api.connections as connections_module
from app.core.crypto import decrypt_secret
from app.engine import schema_rag
from app.engine.db_adapters.base import ColumnSchema, TableSchema
from app.models import DatabaseConnection

from .conftest import auth_headers, signup

DB_PASSWORD = "super-secret-db-password-9999"


class FakeAdapter:
    """Stands in for a real driver. Records the ConnectionInfo it was handed
    so tests can assert the decrypted password actually reached it."""

    def __init__(self, ok=True, error=None, tables=None, introspect_error=None):
        self.ok = ok
        self.error = error
        self.tables = (
            tables
            if tables is not None
            else [
                TableSchema(
                    name="orders",
                    columns=[ColumnSchema(name="id", type="INTEGER", nullable=False, is_pk=True)],
                    sample_rows=[{"id": 1}],
                )
            ]
        )
        self.introspect_error = introspect_error
        self.seen = []

    def test_connection(self, conn):
        self.seen.append(conn)
        return self.ok, self.error

    def introspect_schema(self, conn):
        self.seen.append(conn)
        if self.introspect_error:
            raise RuntimeError(self.introspect_error)
        return self.tables

    def execute_read_only(self, conn, query, max_rows, timeout_seconds):  # pragma: no cover
        return [], []


@pytest.fixture()
def fake_blob(monkeypatch):
    """Stands in for Vercel Blob: an in-memory dict keyed by pathname, with
    `upload_file`/`delete_blob` monkeypatched to read/write/delete it
    instead of making real HTTP calls against blob.vercel-storage.com."""
    store: Dict[str, bytes] = {}

    def _upload_file(pathname, local_path, content_type="application/octet-stream"):
        with open(local_path, "rb") as handle:
            store[pathname] = handle.read()
        return f"fake-blob://{pathname}"

    def _delete_blob(url):
        pathname = url.replace("fake-blob://", "", 1)
        store.pop(pathname, None)

    monkeypatch.setattr(connections_module.blob_storage, "upload_file", _upload_file)
    monkeypatch.setattr(connections_module.blob_storage, "delete_blob", _delete_blob)
    return store


@pytest.fixture()
def fake_stack(monkeypatch):
    """AI configured, a working adapter, and schema indexing stubbed out."""
    adapter = FakeAdapter()
    indexed = []

    monkeypatch.setattr(connections_module, "ai_configured", lambda: True)
    monkeypatch.setattr(connections_module, "get_adapter", lambda engine: adapter)
    monkeypatch.setattr(
        schema_rag,
        "index_connection_schema",
        lambda **kwargs: indexed.append(kwargs) or len(kwargs.get("tables", [])),
    )
    monkeypatch.setattr(schema_rag, "delete_connection_schema", lambda *a, **k: None)

    return {"adapter": adapter, "indexed": indexed}


def _create(client, user, **overrides):
    payload = {
        "name": "Prod analytics",
        "engine": "postgres",
        "host": "db.example.com",
        "port": 5432,
        "database_name": "analytics",
        "username": "reader",
        "password": DB_PASSWORD,
        "extra_params": {"ssl_mode": "require"},
    }
    payload.update(overrides)
    return client.post("/api/connections", json=payload, headers=auth_headers(user))


# --- happy path + status transitions -----------------------------------------


def test_creating_a_connection_indexes_it_and_reports_ready(client, fake_stack):
    user = signup(client)

    response = _create(client, user)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["error_message"] is None
    assert body["schema_indexed_at"] is not None
    assert body["name"] == "Prod analytics"
    assert body["engine"] == "postgres"
    assert body["extra_params"] == {"ssl_mode": "require"}

    # The schema really was handed to the indexer, tagged with this user
    # and this connection.
    assert len(fake_stack["indexed"]) == 1
    assert fake_stack["indexed"][0]["connection_id"] == body["id"]
    assert fake_stack["indexed"][0]["engine_name"] == "postgres"


def test_a_connection_that_cannot_connect_lands_as_failed_not_a_500(client, monkeypatch):
    monkeypatch.setattr(connections_module, "ai_configured", lambda: True)
    monkeypatch.setattr(
        connections_module,
        "get_adapter",
        lambda engine: FakeAdapter(ok=False, error="Could not connect: password authentication failed"),
    )
    user = signup(client)

    response = _create(client, user)

    assert response.status_code == 201  # the request itself never fails
    body = response.json()
    assert body["status"] == "failed"
    assert "password authentication failed" in body["error_message"]
    assert body["schema_indexed_at"] is None


def test_a_schema_introspection_failure_lands_as_failed(client, monkeypatch):
    monkeypatch.setattr(connections_module, "ai_configured", lambda: True)
    monkeypatch.setattr(
        connections_module,
        "get_adapter",
        lambda engine: FakeAdapter(introspect_error="permission denied for schema public"),
    )
    user = signup(client)

    body = _create(client, user).json()

    assert body["status"] == "failed"
    assert "permission denied" in body["error_message"]


def test_an_indexing_failure_lands_as_failed(client, monkeypatch, fake_stack):
    def _boom(**kwargs):
        raise RuntimeError("Postgres (pgvector) unreachable")

    monkeypatch.setattr(schema_rag, "index_connection_schema", _boom)
    user = signup(client)

    body = _create(client, user).json()

    assert body["status"] == "failed"
    assert "Postgres (pgvector) unreachable" in body["error_message"]


def test_a_database_with_no_tables_lands_as_failed_with_an_explanation(
    client, monkeypatch, fake_stack
):
    monkeypatch.setattr(
        connections_module, "get_adapter", lambda engine: FakeAdapter(tables=[])
    )
    user = signup(client)

    body = _create(client, user).json()

    assert body["status"] == "failed"
    assert "no tables" in body["error_message"]


# --- credentials: never in a response, always encrypted at rest --------------


def test_the_password_never_appears_in_any_response(client, fake_stack, db_engine):
    user = signup(client)

    created = _create(client, user)
    assert DB_PASSWORD not in created.text

    listing = client.get("/api/connections", headers=auth_headers(user))
    assert DB_PASSWORD not in listing.text

    detail = client.get(
        f"/api/connections/{created.json()['id']}", headers=auth_headers(user)
    )
    assert DB_PASSWORD not in detail.text

    # Not even a field to hold it, encrypted or otherwise - so a future
    # change can't accidentally start populating one.
    for body in (created.json(), detail.json(), listing.json()[0]):
        assert "password" not in body
        assert "encrypted_password" not in body


def test_the_password_is_stored_encrypted_and_is_decryptable(client, fake_stack, db_engine):
    from sqlalchemy.orm import Session

    user = signup(client)
    connection_id = _create(client, user).json()["id"]

    with Session(db_engine) as session:
        row = session.get(DatabaseConnection, connection_id)
        assert row.encrypted_password
        # Ciphertext, not the password itself...
        assert DB_PASSWORD not in row.encrypted_password
        # ...but it round-trips server-side.
        assert decrypt_secret(row.encrypted_password) == DB_PASSWORD


def test_the_decrypted_password_is_what_reaches_the_adapter(client, fake_stack):
    user = signup(client)
    _create(client, user)

    seen = fake_stack["adapter"].seen
    assert seen, "the adapter should have been asked to connect"
    assert seen[0].password == DB_PASSWORD
    assert seen[0].host == "db.example.com"
    assert seen[0].database == "analytics"
    assert seen[0].extra_params == {"ssl_mode": "require"}


def test_a_connection_without_a_password_is_allowed(client, fake_stack):
    user = signup(client)

    body = _create(client, user, password=None).json()

    assert body["status"] == "ready"
    assert fake_stack["adapter"].seen[0].password is None


# --- config gates --------------------------------------------------------------


def test_creating_a_connection_503s_when_encryption_is_not_configured(
    client, monkeypatch, fake_stack
):
    from app.core.config import get_settings

    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    user = signup(client)

    response = _create(client, user)

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "encryption_not_configured"


def test_creating_a_connection_503s_when_ai_is_not_configured(client, monkeypatch):
    monkeypatch.setattr(connections_module, "ai_configured", lambda: False)
    user = signup(client)

    response = _create(client, user)

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "ai_not_configured"


# --- validation -----------------------------------------------------------------


def test_a_network_engine_requires_a_host(client, fake_stack):
    user = signup(client)
    response = _create(client, user, host="")
    assert response.status_code == 422


def test_sqlite_is_rejected_on_the_json_endpoint_and_points_at_the_upload_route(
    client, fake_stack
):
    user = signup(client)
    response = _create(client, user, engine="sqlite")
    assert response.status_code == 422
    assert "/api/connections/sqlite" in response.text


def test_an_unknown_engine_is_rejected(client, fake_stack):
    user = signup(client)
    response = _create(client, user, engine="oracle")
    assert response.status_code == 422


def test_the_default_port_is_filled_in_when_omitted(client, fake_stack):
    user = signup(client)
    body = _create(client, user, engine="mysql", port=None).json()
    assert body["port"] == 3306


# --- SQLite upload ----------------------------------------------------------------


def test_uploading_a_sqlite_database_stores_the_file_and_records_its_path(
    client, fake_stack, fake_blob
):
    user = signup(client)

    response = client.post(
        "/api/connections/sqlite",
        files={"file": ("mydata.sqlite", b"SQLite format 3\x00stub", "application/octet-stream")},
        data={"name": "My local data"},
        headers=auth_headers(user),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["engine"] == "sqlite"
    assert body["status"] == "ready"
    assert body["host"] is None

    expected_pathname = f"1/{body['id']}/database.sqlite"
    assert fake_blob[expected_pathname] == b"SQLite format 3\x00stub"
    # The blob pathname is derived server-side from the user id and the row
    # id - never from the uploaded filename.
    assert body["extra_params"]["storage_url"] == f"fake-blob://{expected_pathname}"
    assert "mydata.sqlite" not in body["extra_params"]["storage_url"]


def test_uploading_a_sqlite_database_requires_a_name(client, fake_stack, fake_blob):
    user = signup(client)

    response = client.post(
        "/api/connections/sqlite",
        files={"file": ("mydata.sqlite", b"stub", "application/octet-stream")},
        data={"name": "   "},
        headers=auth_headers(user),
    )

    assert response.status_code == 422


# --- listing, test, reindex, delete + ownership -------------------------------


def test_listing_only_returns_the_callers_own_connections(client, fake_stack):
    owner = signup(client)
    other = signup(client)
    _create(client, owner, name="Mine")
    _create(client, other, name="Theirs")

    listing = client.get("/api/connections", headers=auth_headers(owner)).json()

    assert [c["name"] for c in listing] == ["Mine"]


def test_getting_another_users_connection_404s(client, fake_stack):
    owner = signup(client)
    intruder = signup(client)
    connection_id = _create(client, owner).json()["id"]

    response = client.get(
        f"/api/connections/{connection_id}", headers=auth_headers(intruder)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Connection not found"


def test_a_connection_that_never_existed_404s_identically(client, fake_stack):
    user = signup(client)
    response = client.get("/api/connections/999999", headers=auth_headers(user))
    assert response.status_code == 404
    assert response.json()["detail"] == "Connection not found"


def test_test_endpoint_reports_ok_without_reindexing(client, fake_stack):
    user = signup(client)
    connection_id = _create(client, user).json()["id"]
    indexed_before = len(fake_stack["indexed"])

    response = client.post(
        f"/api/connections/{connection_id}/test", headers=auth_headers(user)
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "error": None}
    assert len(fake_stack["indexed"]) == indexed_before  # no re-indexing happened


def test_test_endpoint_reports_a_failure_without_changing_status(client, monkeypatch, fake_stack):
    user = signup(client)
    connection_id = _create(client, user).json()["id"]

    monkeypatch.setattr(
        connections_module,
        "get_adapter",
        lambda engine: FakeAdapter(ok=False, error="Could not connect: host is down"),
    )
    response = client.post(
        f"/api/connections/{connection_id}/test", headers=auth_headers(user)
    )

    assert response.json()["ok"] is False
    assert "host is down" in response.json()["error"]
    # A momentarily-unreachable database must not demote a ready connection.
    detail = client.get(f"/api/connections/{connection_id}", headers=auth_headers(user))
    assert detail.json()["status"] == "ready"


def test_test_endpoint_404s_for_another_users_connection(client, fake_stack):
    owner = signup(client)
    intruder = signup(client)
    connection_id = _create(client, owner).json()["id"]

    response = client.post(
        f"/api/connections/{connection_id}/test", headers=auth_headers(intruder)
    )

    assert response.status_code == 404


def test_reindex_clears_the_old_points_and_reindexes(client, fake_stack, monkeypatch):
    deleted = []
    monkeypatch.setattr(
        schema_rag,
        "delete_connection_schema",
        lambda user_id, connection_id, **kwargs: deleted.append((user_id, connection_id)),
    )
    user = signup(client)
    connection_id = _create(client, user).json()["id"]
    indexed_before = len(fake_stack["indexed"])

    response = client.post(
        f"/api/connections/{connection_id}/reindex", headers=auth_headers(user)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert len(fake_stack["indexed"]) == indexed_before + 1
    assert deleted[-1][1] == connection_id


def test_reindex_404s_for_another_users_connection(client, fake_stack):
    owner = signup(client)
    intruder = signup(client)
    connection_id = _create(client, owner).json()["id"]

    response = client.post(
        f"/api/connections/{connection_id}/reindex", headers=auth_headers(intruder)
    )

    assert response.status_code == 404


def test_delete_removes_the_row_and_its_schema_points(client, fake_stack, monkeypatch):
    deleted = []
    monkeypatch.setattr(
        schema_rag,
        "delete_connection_schema",
        lambda user_id, connection_id, **kwargs: deleted.append((user_id, connection_id)),
    )
    user = signup(client)
    connection_id = _create(client, user).json()["id"]

    response = client.delete(
        f"/api/connections/{connection_id}", headers=auth_headers(user)
    )

    assert response.status_code == 204
    assert deleted[-1][1] == connection_id
    assert (
        client.get(f"/api/connections/{connection_id}", headers=auth_headers(user)).status_code
        == 404
    )


def test_delete_404s_for_another_users_connection_and_leaves_it_intact(client, fake_stack):
    owner = signup(client)
    intruder = signup(client)
    connection_id = _create(client, owner).json()["id"]

    response = client.delete(
        f"/api/connections/{connection_id}", headers=auth_headers(intruder)
    )

    assert response.status_code == 404
    still_there = client.get(
        f"/api/connections/{connection_id}", headers=auth_headers(owner)
    )
    assert still_there.status_code == 200


def test_deleting_a_sqlite_connection_removes_its_stored_file(
    client, fake_stack, fake_blob, monkeypatch
):
    monkeypatch.setattr(schema_rag, "delete_connection_schema", lambda *a, **k: None)
    user = signup(client)

    created = client.post(
        "/api/connections/sqlite",
        files={"file": ("d.sqlite", b"stub", "application/octet-stream")},
        data={"name": "Local"},
        headers=auth_headers(user),
    ).json()
    pathname = f"1/{created['id']}/database.sqlite"
    assert pathname in fake_blob

    client.delete(f"/api/connections/{created['id']}", headers=auth_headers(user))

    assert pathname not in fake_blob


def test_connection_endpoints_require_authentication(client):
    assert client.get("/api/connections").status_code == 401
    assert client.post("/api/connections", json={}).status_code == 401
