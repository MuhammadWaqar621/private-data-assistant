"""SQLAlchemy models.

Importing this package registers every model on `Base.metadata`, which is
what Alembic's env.py relies on for autogeneration - always import
`app.models` (not individual model modules) before touching
`Base.metadata` for migrations.
"""

from app.models.chat import Chat
from app.models.database_connection import (
    ConnectionStatus,
    DatabaseConnection,
    DatabaseEngine,
)
from app.models.message import Message, MessageRole
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User

__all__ = [
    "User",
    "PasswordResetToken",
    "DatabaseConnection",
    "DatabaseEngine",
    "ConnectionStatus",
    "Chat",
    "Message",
    "MessageRole",
]
