"""initial schema: users, password_reset_tokens, database_connections, chats, messages

Creates this application's own metadata database. Note what is NOT here:
anything resembling the user's business data. `database_connections` stores
only how to reach a user's database plus their encrypted password;
`messages.query_sql` stores the query text that ran, not its results (only
a chart's own rows are persisted, in `messages.chart_spec`, so history can
re-render it).

Table order matters for the foreign keys: users -> database_connections ->
chats -> messages.

Revision ID: a1f4c7d2e930
Revises:
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f4c7d2e930'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('full_name', sa.String(), nullable=True),
        sa.Column('hashed_password', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)

    op.create_table(
        'password_reset_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_password_reset_tokens_id'), 'password_reset_tokens', ['id'], unique=False
    )
    op.create_index(
        op.f('ix_password_reset_tokens_token'), 'password_reset_tokens', ['token'], unique=True
    )
    op.create_index(
        op.f('ix_password_reset_tokens_user_id'),
        'password_reset_tokens',
        ['user_id'],
        unique=False,
    )

    op.create_table(
        'database_connections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column(
            'engine',
            sa.Enum(
                'postgres', 'mysql', 'mssql', 'sqlite', 'mongodb', name='database_engine'
            ),
            nullable=False,
        ),
        sa.Column('host', sa.String(), nullable=True),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('database_name', sa.String(), nullable=False),
        sa.Column('username', sa.String(), nullable=True),
        # Fernet ciphertext (app/core/crypto.py) - never plaintext, and
        # never returned by any API response.
        sa.Column('encrypted_password', sa.Text(), nullable=True),
        sa.Column('extra_params', sa.JSON(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('pending', 'indexing', 'ready', 'failed', name='connection_status'),
            nullable=False,
        ),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('schema_indexed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_database_connections_id'), 'database_connections', ['id'], unique=False
    )
    op.create_index(
        op.f('ix_database_connections_user_id'),
        'database_connections',
        ['user_id'],
        unique=False,
    )

    op.create_table(
        'chats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        # Nullable: a chat can exist before a database is picked. SET NULL
        # (not CASCADE) on delete so removing a connection keeps the
        # transcript but unbinds it.
        sa.Column('connection_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['connection_id'], ['database_connections.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chats_id'), 'chats', ['id'], unique=False)
    op.create_index(op.f('ix_chats_user_id'), 'chats', ['user_id'], unique=False)
    op.create_index(op.f('ix_chats_connection_id'), 'chats', ['connection_id'], unique=False)

    op.create_table(
        'messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.Enum('user', 'assistant', name='message_role'), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('query_sql', sa.Text(), nullable=True),
        sa.Column('chart_spec', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_messages_chat_id'), 'messages', ['chat_id'], unique=False)
    op.create_index(op.f('ix_messages_id'), 'messages', ['id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_messages_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_chat_id'), table_name='messages')
    op.drop_table('messages')

    op.drop_index(op.f('ix_chats_connection_id'), table_name='chats')
    op.drop_index(op.f('ix_chats_user_id'), table_name='chats')
    op.drop_index(op.f('ix_chats_id'), table_name='chats')
    op.drop_table('chats')

    op.drop_index(op.f('ix_database_connections_user_id'), table_name='database_connections')
    op.drop_index(op.f('ix_database_connections_id'), table_name='database_connections')
    op.drop_table('database_connections')

    op.drop_index(op.f('ix_password_reset_tokens_user_id'), table_name='password_reset_tokens')
    op.drop_index(op.f('ix_password_reset_tokens_token'), table_name='password_reset_tokens')
    op.drop_index(op.f('ix_password_reset_tokens_id'), table_name='password_reset_tokens')
    op.drop_table('password_reset_tokens')

    op.drop_index(op.f('ix_users_id'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')

    # Postgres keeps the enum types behind after their tables are dropped.
    sa.Enum(name='connection_status').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='database_engine').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='message_role').drop(op.get_bind(), checkfirst=True)
