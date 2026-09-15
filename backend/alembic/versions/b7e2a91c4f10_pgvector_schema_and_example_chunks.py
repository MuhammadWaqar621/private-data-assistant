"""pgvector: schema_chunks and example_chunks tables

Replaces the two Qdrant collections this project used to run
(`private_data_assistant_schema` and `private_data_assistant_examples`)
with two tables in THIS app's own Postgres database, using the `vector`
extension - see app/engine/vector_store.py for the SQLAlchemy Core table
definitions these mirror, and app/engine/schema_rag.py / example_rag.py for
the upsert/search/delete logic that now targets them instead of a vector
service.

Vercel Postgres (Neon-backed) supports `CREATE EXTENSION vector` natively,
which is the whole point of this migration: one managed Postgres database
holds this app's relational metadata AND its embeddings, with no second
vector service to provision.

The embedding column is sized to AZURE_EM_DIMENSIONS (default 1536, same
default `app/engine/azure_client.get_embedding_dimensions()` uses) - if
that env var is changed to match a different embedding model AFTER this
migration has already run, both tables must be re-created (there is no
in-place resize of a pgvector column's dimension) and every connection
re-indexed. This mirrors exactly the constraint the old Qdrant collection
had (it was sized once, at first creation, from the same setting).

An IVFFlat index is added on each embedding column for approximate nearest-
neighbor search - fine at this project's scale (the isolation WHERE clause
on user_id/connection_id narrows the candidate set long before the index
even matters much), and it's the pgvector index type with the broadest
version support. `lists = 100` is a reasonable default for a table this
project expects to hold at most thousands, not millions, of rows; revisit
if a deployment's row count grows far beyond that.

Revision ID: b7e2a91c4f10
Revises: a1f4c7d2e930
Create Date: 2026-09-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from app.engine.azure_client import get_embedding_dimensions

# revision identifiers, used by Alembic.
revision: str = 'b7e2a91c4f10'
down_revision: Union[str, None] = 'a1f4c7d2e930'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    dimensions = get_embedding_dimensions()

    op.create_table(
        'schema_chunks',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('engine', sa.String(), nullable=False),
        sa.Column('table_name', sa.String(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('embedding', Vector(dimensions), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_schema_chunks_user_connection',
        'schema_chunks',
        ['user_id', 'connection_id'],
        unique=False,
    )
    op.execute(
        'CREATE INDEX ix_schema_chunks_embedding ON schema_chunks '
        'USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)'
    )

    op.create_table(
        'example_chunks',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('engine', sa.String(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('query', sa.Text(), nullable=False),
        sa.Column('seeded', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('embedding', Vector(dimensions), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_example_chunks_user_connection',
        'example_chunks',
        ['user_id', 'connection_id'],
        unique=False,
    )
    op.execute(
        'CREATE INDEX ix_example_chunks_embedding ON example_chunks '
        'USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)'
    )


def downgrade() -> None:
    op.drop_index('ix_example_chunks_embedding', table_name='example_chunks')
    op.drop_index('ix_example_chunks_user_connection', table_name='example_chunks')
    op.drop_table('example_chunks')

    op.drop_index('ix_schema_chunks_embedding', table_name='schema_chunks')
    op.drop_index('ix_schema_chunks_user_connection', table_name='schema_chunks')
    op.drop_table('schema_chunks')

    # Deliberately NOT dropping the `vector` extension itself - another
    # object in the database may depend on it, and CREATE EXTENSION is
    # idempotent/harmless to leave behind either way.
