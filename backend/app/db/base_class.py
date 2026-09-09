"""
Declarative base for all SQLAlchemy models.

Kept in its own tiny module (separate from session.py and models/) so that
Alembic's env.py can import `Base` and get the full `Base.metadata` for
autogeneration without needing to import the engine/session machinery.

Note: this Base describes THIS application's own metadata database only.
The user's registered external databases are never modelled here - they
are introspected live by app/engine/db_adapters/ and only ever read.
"""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
