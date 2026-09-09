"""
Integration tests for /api/chats/* - CRUD, the ownership check the rest of
the app builds on (another user's chat must 404, indistinguishable from one
that never existed), and the connection binding.

`connection_id` is the field with teeth here: a chat may be created or
PATCHed to point at a database connection, and doing so must verify the
connection belongs to the CALLER. It is also nullable throughout - a chat
exists before a database is picked, and can be unbound again.

Connections are inserted straight into the test database rather than
through /api/connections, so these tests don't depend on the adapter or
schema-indexing stubs (test_connections_api.py covers that endpoint).
"""

from sqlalchemy.orm import Session

from app.models import ConnectionStatus, DatabaseConnection, DatabaseEngine, User

from .conftest import auth_headers, signup


def _create_chat(client, token_body, **payload):
    response = client.post("/api/chats", json=payload, headers=auth_headers(token_body))
    assert response.status_code in (200, 201), response.text
    return response.json()


def _insert_connection(db_engine, email: str, name: str = "Their DB") -> int:
    """Insert a ready connection owned by the user with this email."""
    with Session(db_engine) as session:
        user = session.query(User).filter(User.email == email).one()
        connection = DatabaseConnection(
            user_id=user.id,
            name=name,
            engine=DatabaseEngine.postgres,
            host="db.example.com",
            port=5432,
            database_name="analytics",
            username="reader",
            encrypted_password=None,
            extra_params={},
            status=ConnectionStatus.ready,
        )
        session.add(connection)
        session.commit()
        return connection.id


# --- basic CRUD ---------------------------------------------------------------


def test_create_chat_defaults_title_and_has_no_connection(client):
    user = signup(client)
    chat = _create_chat(client, user)
    assert chat["title"] == "New chat"
    assert chat["connection_id"] is None


def test_create_chat_with_custom_title(client):
    user = signup(client)
    chat = _create_chat(client, user, title="Q3 revenue")
    assert chat["title"] == "Q3 revenue"


def test_create_chat_reuses_an_existing_empty_untitled_unbound_chat(client):
    user = signup(client)
    first = _create_chat(client, user)

    response = client.post("/api/chats", json={}, headers=auth_headers(user))

    assert response.status_code == 200  # reused, nothing new created
    assert response.json()["id"] == first["id"]
    assert len(client.get("/api/chats", headers=auth_headers(user)).json()) == 1


def test_create_chat_with_a_connection_is_never_reused(client, db_engine):
    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    first = _create_chat(client, user)

    second = client.post(
        "/api/chats", json={"connection_id": connection_id}, headers=auth_headers(user)
    )

    assert second.status_code == 201
    assert second.json()["id"] != first["id"]
    assert second.json()["connection_id"] == connection_id


def test_list_chats_only_returns_the_current_users_chats(client):
    user_a = signup(client)
    user_b = signup(client)
    _create_chat(client, user_a, title="A's chat")
    _create_chat(client, user_b, title="B's chat")

    listing = client.get("/api/chats", headers=auth_headers(user_a)).json()

    assert [c["title"] for c in listing] == ["A's chat"]


def test_get_chat_returns_its_messages(client):
    user = signup(client)
    chat = _create_chat(client, user)

    body = client.get(f"/api/chats/{chat['id']}", headers=auth_headers(user)).json()

    assert body["id"] == chat["id"]
    assert body["messages"] == []
    assert body["connection_id"] is None


# --- ownership: another user's chat must 404, not 403 ------------------------


def test_get_chat_404s_for_another_users_chat(client):
    owner = signup(client)
    intruder = signup(client)
    chat = _create_chat(client, owner)

    response = client.get(f"/api/chats/{chat['id']}", headers=auth_headers(intruder))

    assert response.status_code == 404
    assert response.json()["detail"] == "Chat not found"


def test_get_chat_404s_for_a_chat_that_never_existed(client):
    user = signup(client)
    response = client.get("/api/chats/999999", headers=auth_headers(user))
    assert response.status_code == 404
    assert response.json()["detail"] == "Chat not found"


def test_delete_chat_404s_for_another_users_chat(client):
    owner = signup(client)
    intruder = signup(client)
    chat = _create_chat(client, owner)

    assert (
        client.delete(f"/api/chats/{chat['id']}", headers=auth_headers(intruder)).status_code
        == 404
    )
    assert (
        client.get(f"/api/chats/{chat['id']}", headers=auth_headers(owner)).status_code == 200
    )


def test_delete_chat_succeeds_for_its_owner(client):
    owner = signup(client)
    chat = _create_chat(client, owner)

    assert (
        client.delete(f"/api/chats/{chat['id']}", headers=auth_headers(owner)).status_code
        == 204
    )
    assert client.get(f"/api/chats/{chat['id']}", headers=auth_headers(owner)).status_code == 404


def test_chats_endpoints_require_authentication(client):
    assert client.get("/api/chats").status_code == 401


# --- setting / changing / clearing the connection ----------------------------


def test_patch_sets_the_connection(client, db_engine):
    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    chat = _create_chat(client, user)

    response = client.patch(
        f"/api/chats/{chat['id']}",
        json={"connection_id": connection_id},
        headers=auth_headers(user),
    )

    assert response.status_code == 200
    assert response.json()["connection_id"] == connection_id
    # And it sticks.
    assert (
        client.get(f"/api/chats/{chat['id']}", headers=auth_headers(user)).json()[
            "connection_id"
        ]
        == connection_id
    )


def test_patch_can_change_the_connection(client, db_engine):
    user = signup(client)
    first = _insert_connection(db_engine, user["email"], name="Prod")
    second = _insert_connection(db_engine, user["email"], name="Staging")
    chat = _create_chat(client, user, connection_id=first)

    response = client.patch(
        f"/api/chats/{chat['id']}", json={"connection_id": second}, headers=auth_headers(user)
    )

    assert response.json()["connection_id"] == second


def test_patch_with_an_explicit_null_clears_the_connection(client, db_engine):
    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    chat = _create_chat(client, user, connection_id=connection_id)

    response = client.patch(
        f"/api/chats/{chat['id']}", json={"connection_id": None}, headers=auth_headers(user)
    )

    assert response.status_code == 200
    assert response.json()["connection_id"] is None


def test_patching_only_the_title_leaves_the_connection_alone(client, db_engine):
    """Presence, not None-ness, is what marks a field as "being changed" -
    omitting connection_id must not silently unbind the chat."""
    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    chat = _create_chat(client, user, connection_id=connection_id)

    response = client.patch(
        f"/api/chats/{chat['id']}", json={"title": "Renamed"}, headers=auth_headers(user)
    )

    assert response.json()["title"] == "Renamed"
    assert response.json()["connection_id"] == connection_id


def test_patch_rejects_a_blank_title(client):
    user = signup(client)
    chat = _create_chat(client, user)

    response = client.patch(
        f"/api/chats/{chat['id']}", json={"title": "   "}, headers=auth_headers(user)
    )

    assert response.status_code == 422


# --- the security-relevant part: you can only bind YOUR OWN connection -------


def test_patch_404s_when_binding_to_another_users_connection(client, db_engine):
    owner = signup(client)
    intruder = signup(client)
    connection_id = _insert_connection(db_engine, owner["email"], name="Owner's DB")
    intruder_chat = _create_chat(client, intruder)

    response = client.patch(
        f"/api/chats/{intruder_chat['id']}",
        json={"connection_id": connection_id},
        headers=auth_headers(intruder),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Connection not found"
    # And nothing was bound.
    assert (
        client.get(f"/api/chats/{intruder_chat['id']}", headers=auth_headers(intruder)).json()[
            "connection_id"
        ]
        is None
    )


def test_creating_a_chat_bound_to_another_users_connection_404s(client, db_engine):
    owner = signup(client)
    intruder = signup(client)
    connection_id = _insert_connection(db_engine, owner["email"])

    response = client.post(
        "/api/chats", json={"connection_id": connection_id}, headers=auth_headers(intruder)
    )

    assert response.status_code == 404


def test_patch_404s_for_a_connection_that_never_existed(client):
    user = signup(client)
    chat = _create_chat(client, user)

    response = client.patch(
        f"/api/chats/{chat['id']}", json={"connection_id": 999999}, headers=auth_headers(user)
    )

    assert response.status_code == 404


def test_patch_404s_for_another_users_chat(client):
    owner = signup(client)
    intruder = signup(client)
    chat = _create_chat(client, owner)

    response = client.patch(
        f"/api/chats/{chat['id']}", json={"title": "Hijacked"}, headers=auth_headers(intruder)
    )

    assert response.status_code == 404
