"""
Authentication endpoints: email/password signup+login, JWT refresh, and
forgot/reset-password (via SMTP).

SMTP email delivery depends on env vars that may not be set in every
deployment (see app/api/config_status.py) - the endpoints that need it
check first and return a clear 503 JSON error instead of crashing,
mirroring the config-status pattern used elsewhere in this project.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.email import send_password_reset_email, smtp_configured
from app.core.security import (
    JWTNotConfiguredError,
    WeakPasswordError,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_password_reset_token,
    hash_password,
    require_jwt_configured,
    validate_password_strength,
    verify_password,
)
from app.db.session import get_db
from app.models import PasswordResetToken, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --- Schemas -----------------------------------------------------------------


class SignupRequest(BaseModel):
    email: EmailStr
    full_name: str
    password: str

    @field_validator("full_name")
    @classmethod
    def _check_full_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Full name is required")
        return stripped

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        try:
            validate_password_strength(value)
        except WeakPasswordError as exc:
            raise ValueError(str(exc)) from exc
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        try:
            validate_password_strength(value)
        except WeakPasswordError as exc:
            raise ValueError(str(exc)) from exc
        return value


class MessageResponse(BaseModel):
    message: str


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None = None

    model_config = {"from_attributes": True}


# --- Helpers -------------------------------------------------------------


def _tokens_for(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


def _service_unavailable(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": error, "message": message},
    )


# --- Signup / login / refresh -------------------------------------------


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, db: Session = Depends(get_db)) -> TokenResponse:
    try:
        require_jwt_configured()
    except JWTNotConfiguredError as exc:
        raise _service_unavailable("jwt_not_configured", str(exc))

    existing = db.query(User).filter(User.email == body.email).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered"
        )

    user = User(
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return _tokens_for(user)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    try:
        require_jwt_configured()
    except JWTNotConfiguredError as exc:
        raise _service_unavailable("jwt_not_configured", str(exc))

    user = db.query(User).filter(User.email == body.email).first()
    if (
        user is None
        or not user.hashed_password
        or not verify_password(body.password, user.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )

    return _tokens_for(user)


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> AccessTokenResponse:
    try:
        payload = decode_token(body.refresh_token)
    except JWTNotConfiguredError as exc:
        raise _service_unavailable("jwt_not_configured", str(exc))
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not a refresh token"
        )

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject"
        )

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists"
        )

    return AccessTokenResponse(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


# --- Forgot / reset password (SMTP) ---------------------------------------


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MessageResponse:
    if not smtp_configured(settings):
        raise _service_unavailable(
            "smtp_not_configured",
            "Email sending is not configured. Set SMTP_* variables in .env.",
        )

    user = db.query(User).filter(User.email == body.email).first()

    # Always return the same generic response whether or not the email is
    # registered, so this endpoint can't be used to enumerate accounts.
    generic_response = MessageResponse(
        message="If that email is registered, a password reset link has been sent."
    )

    if user is None:
        return generic_response

    reset_token = PasswordResetToken(
        user_id=user.id,
        token=generate_password_reset_token(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        used=False,
    )
    db.add(reset_token)
    db.commit()

    reset_link = f"{settings.FRONTEND_URL}/reset-password?token={reset_token.token}"

    try:
        await send_password_reset_email(user.email, reset_link)
    except Exception as exc:  # noqa: BLE001 - surface any SMTP failure clearly, don't crash
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "email_send_failed", "message": str(exc)},
        )

    return generic_response


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)) -> MessageResponse:
    reset_token = (
        db.query(PasswordResetToken).filter(PasswordResetToken.token == body.token).first()
    )

    if (
        reset_token is None
        or reset_token.used
        or reset_token.expires_at < datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token"
        )

    user = db.get(User, reset_token.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token"
        )

    user.hashed_password = hash_password(body.new_password)
    reset_token.used = True
    db.commit()

    return MessageResponse(
        message="Password updated. You can now log in with your new password."
    )
