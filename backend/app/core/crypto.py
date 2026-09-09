"""
Symmetric encryption for the credentials of the user's *own* external
databases (`database_connections.encrypted_password`).

Why encryption and not hashing: unlike this app's own account passwords
(app/core/security.py, hashed one-way with bcrypt), a registered
database's password has to be handed back to a database driver verbatim
every time a query runs - so it must be recoverable. It is therefore
encrypted at rest with `cryptography.fernet.Fernet` (AES-128-CBC +
HMAC-SHA256 authenticated encryption) under a single deployment-wide key
supplied via the ENCRYPTION_KEY environment variable.

Operational rules this module exists to make easy to follow:

  - ENCRYPTION_KEY is REQUIRED before any connection can be registered.
    `POST /api/connections` returns a 503 (`encryption_not_configured`)
    rather than storing a plaintext password or crashing.
  - Ciphertext goes into Postgres; the plaintext is only ever materialized
    in memory, immediately before a driver connection is opened (see
    app/api/connections.py and app/api/messages.py).
  - No API response ever contains either form. `ConnectionOut` in
    app/api/connections.py has no password field at all - not a redacted
    one, not a null one - so there is nothing to accidentally serialize.
  - Rotating ENCRYPTION_KEY invalidates every stored password (they can no
    longer be decrypted); connections then report `status=failed` on their
    next test/reindex and have to be re-saved with their password. There
    is deliberately no key-rotation tooling in this pass - see the README's
    "Roadmap / known tradeoffs".
"""

from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class EncryptionNotConfiguredError(RuntimeError):
    """Raised when encryption is needed but ENCRYPTION_KEY isn't set (or
    isn't a valid 32-byte urlsafe-base64 Fernet key)."""


class DecryptionError(RuntimeError):
    """Raised when a stored ciphertext cannot be decrypted with the current
    ENCRYPTION_KEY - typically because the key was rotated or the value was
    written by a different deployment."""


def generate_encryption_key() -> str:
    """A fresh urlsafe-base64 Fernet key. Exposed as a helper so the README
    can document one canonical command:

        python -c "from app.core.crypto import generate_encryption_key; print(generate_encryption_key())"

    (The dependency-free equivalent documented in .env.example is
    `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.)
    """
    return Fernet.generate_key().decode()


def encryption_configured(key: Optional[str] = None) -> bool:
    """True only when ENCRYPTION_KEY is set AND is a usable Fernet key -
    a truthy-but-malformed value is treated as unconfigured, so the 503
    gate fires at request time instead of a 500 at encrypt time."""
    raw = key if key is not None else (get_settings().ENCRYPTION_KEY or "")
    raw = raw.strip()
    if not raw:
        return False
    try:
        Fernet(raw.encode())
    except Exception:  # noqa: BLE001 - any malformed key means "not configured"
        return False
    return True


@lru_cache
def _fernet_for(key: str) -> Fernet:
    return Fernet(key.encode())


def _get_fernet() -> Fernet:
    raw = (get_settings().ENCRYPTION_KEY or "").strip()
    if not raw:
        raise EncryptionNotConfiguredError(
            "ENCRYPTION_KEY is not set - database connection credentials "
            "cannot be encrypted. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())" and set it in .env.'
        )
    try:
        return _fernet_for(raw)
    except Exception as exc:  # noqa: BLE001
        raise EncryptionNotConfiguredError(
            "ENCRYPTION_KEY is not a valid Fernet key (it must be 32 bytes, "
            f"urlsafe-base64 encoded): {exc}"
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a credential for storage. Returns urlsafe-base64 text safe
    to put in a `String`/`Text` column."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored credential. Call this as late as possible - ideally
    on the line before the driver connection is opened - and never put the
    result anywhere it could be logged or serialized."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Stored credential could not be decrypted with the current "
            "ENCRYPTION_KEY. If the key was rotated, re-save this "
            "connection with its password."
        ) from exc
