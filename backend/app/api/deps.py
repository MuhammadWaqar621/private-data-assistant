"""
Shared FastAPI dependencies for the API layer.

`get_current_user` protects every non-public endpoint - it reads the
`Authorization: Bearer <access_token>` header, verifies the JWT, and loads
the corresponding User row. Every ownership check in this app is then
`row.user_id != current_user.id -> 404` (never 403, so a resource
belonging to someone else is indistinguishable from one that never
existed).
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.security import JWTNotConfiguredError, decode_token
from app.db.session import get_db
from app.models import User

# tokenUrl is only used to populate the "Authorize" button in /docs; this
# project's login endpoint takes JSON, not an OAuth2 password form, so the
# built-in form-based flow in Swagger UI won't actually work end-to-end -
# paste a bearer token manually there instead.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        raise credentials_exception

    try:
        payload = decode_token(token)
    except JWTNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except JWTError:
        raise credentials_exception

    if payload.get("type") != "access":
        raise credentials_exception

    raw_user_id = payload.get("sub")
    if raw_user_id is None:
        raise credentials_exception

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        raise credentials_exception

    user = db.get(User, user_id)
    if user is None:
        raise credentials_exception

    return user
