"""
SMTP email sending, used only by the forgot-password flow.

Callers must check `smtp_configured()` first - `send_password_reset_email`
assumes all SMTP_* settings are present and will raise if they aren't.
"""

from email.message import EmailMessage

import aiosmtplib

from app.core.config import Settings, get_settings


def smtp_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.SMTP_HOST
        and settings.SMTP_PORT
        and settings.SMTP_USERNAME
        and settings.SMTP_PASSWORD
        and settings.SMTP_FROM_EMAIL
    )


async def send_password_reset_email(to_email: str, reset_link: str) -> None:
    settings = get_settings()

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = to_email
    message["Subject"] = "Reset your Private Data Assistant password"
    message.set_content(
        "We received a request to reset your Private Data Assistant password.\n\n"
        f"Reset it here: {reset_link}\n\n"
        "This link expires in 1 hour. If you didn't request this, you can "
        "safely ignore this email."
    )

    await aiosmtplib.send(
        message,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USERNAME,
        password=settings.SMTP_PASSWORD,
        start_tls=True,
    )
