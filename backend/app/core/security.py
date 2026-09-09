"""
Password hashing and JWT helpers used by app/api/auth.py and
app/api/deps.py.

Access tokens carry `type: "access"` and expire after
JWT_ACCESS_TOKEN_EXPIRE_MINUTES; refresh tokens carry `type: "refresh"` and
expire after JWT_REFRESH_TOKEN_EXPIRE_DAYS. Both carry `sub` = the user id
(as a string, per JWT convention). Distinguishing by `type` stops a
refresh token from being replayed as an access token if it leaks.

Credential encryption for the user's *external* database passwords is a
separate concern and lives in app/core/crypto.py - passwords here are
hashed (one-way, for this app's own accounts), passwords there are
encrypted (reversible, because we have to hand them to a database driver).
"""

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

MIN_PASSWORD_LENGTH = 8

# Enforced server-side (the real security boundary - client-side checks
# are just a UX convenience and can't be trusted) on both signup and
# reset-password. Requires: minimum length, at least one uppercase letter,
# one lowercase letter, one digit, and one special (non-alphanumeric)
# character.
_UPPERCASE_RE = re.compile(r"[A-Z]")
_LOWERCASE_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"[0-9]")
_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


class WeakPasswordError(ValueError):
    """Raised when a password doesn't meet the minimum strength policy."""


def validate_password_strength(password: str) -> None:
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"at least {MIN_PASSWORD_LENGTH} characters")
    if not _UPPERCASE_RE.search(password):
        problems.append("an uppercase letter")
    if not _LOWERCASE_RE.search(password):
        problems.append("a lowercase letter")
    if not _DIGIT_RE.search(password):
        problems.append("a number")
    if not _SPECIAL_RE.search(password):
        problems.append("a special character")

    if problems:
        raise WeakPasswordError("Password must contain " + ", ".join(problems) + ".")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


class JWTNotConfiguredError(RuntimeError):
    """Raised when a JWT is requested but JWT_SECRET_KEY isn't set."""


def _require_secret() -> str:
    settings = get_settings()
    if not settings.JWT_SECRET_KEY:
        raise JWTNotConfiguredError(
            "JWT_SECRET_KEY is not set - see .env.example and set a real "
            "value in .env before using auth endpoints."
        )
    return settings.JWT_SECRET_KEY


def _create_token(user_id: int, expires_delta: timedelta, token_type: str) -> str:
    settings = get_settings()
    secret = _require_secret()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, secret, algorithm=settings.JWT_ALGORITHM)


def require_jwt_configured() -> None:
    """Raise JWTNotConfiguredError early (before doing any DB work) if
    JWT_SECRET_KEY isn't set. Used by signup/login to fail fast and
    clearly rather than after already touching the database."""
    _require_secret()


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    return _create_token(
        user_id, timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES), "access"
    )


def create_refresh_token(user_id: int) -> str:
    settings = get_settings()
    return _create_token(
        user_id, timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS), "refresh"
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises jose.JWTError on any invalid/expired
    token, and JWTNotConfiguredError if JWT_SECRET_KEY isn't set."""
    settings = get_settings()
    secret = _require_secret()
    return jwt.decode(token, secret, algorithms=[settings.JWT_ALGORITHM])


def generate_password_reset_token() -> str:
    """A high-entropy, URL-safe token for the password_reset_tokens table."""
    return secrets.token_urlsafe(32)
