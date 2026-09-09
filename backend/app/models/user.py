"""User account model.

Authentication is email/password only - `hashed_password` is set at
signup and verified at login.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    # Nullable at the DB level (nothing here should refuse to load an
    # account that has none) even though app/api/auth.py's SignupRequest
    # requires it for every new signup.
    full_name = Column(String, nullable=True)
    hashed_password = Column(String, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    chats = relationship("Chat", back_populates="user", cascade="all, delete-orphan")
    connections = relationship(
        "DatabaseConnection", back_populates="user", cascade="all, delete-orphan"
    )
    password_reset_tokens = relationship(
        "PasswordResetToken", back_populates="user", cascade="all, delete-orphan"
    )
