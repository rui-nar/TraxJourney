"""Email-verification tokens (issue #110).

Registration stores whatever address the user typed. Verification is what turns
that self-declared string into evidence that they can actually read mail there —
without it, anyone could register with someone else's address and claim a
pending invite meant for them.

The gate is deliberately **soft**: an unverified account is fully usable. It may
not send invite emails and is not matched to a pending invite, and nothing else
changes. A hard gate would make the mail relay a hard dependency of signup, so
a Brevo outage or a message in a spam folder would mean nobody can register.
"""
from __future__ import annotations

import logging
import os
import time

from sqlmodel import select

from models.user import EmailVerification, UserInfo
from src.brand import APP_NAME
from src.email.address import normalize_email
from src.email.service import EmailMessage, get_email_service
from src.email.templates import render_verification_email

_logger = logging.getLogger(__name__)

TOKEN_TTL_HOURS = 24
_TOKEN_TTL_SECONDS = TOKEN_TTL_HOURS * 3600


def _frontend_origin() -> str:
    """Base URL for the link in the email.

    Read at call time, not import time, so tests and ops can set it without
    reimporting the module.
    """
    return os.environ.get("FRONTEND_ORIGIN", "http://localhost:5500")


def issue_token(sess, user_info: UserInfo) -> EmailVerification:
    """Create a fresh verification token for *user_info*, replacing any existing
    one so an older link stops working the moment a new one is requested."""
    for stale in sess.exec(
        select(EmailVerification).where(
            EmailVerification.user_info_id == user_info.id)
    ).all():
        sess.delete(stale)
    sess.commit()

    row = EmailVerification(
        user_info_id=user_info.id,
        email=normalize_email(user_info.email),
        expires_at=time.time() + _TOKEN_TTL_SECONDS,
    )
    sess.add(row)
    sess.commit()
    sess.refresh(row)
    return row


def consume_token(sess, token: str) -> UserInfo | None:
    """Mark the token's account verified and delete the token.

    Returns the updated ``UserInfo``, or None when the token is unknown,
    expired, or was issued for an address the account no longer carries.
    """
    row = sess.exec(
        select(EmailVerification).where(EmailVerification.token == token)
    ).first()
    if row is None:
        return None

    if row.expires_at and row.expires_at < time.time():
        sess.delete(row)
        sess.commit()
        return None

    user = sess.get(UserInfo, row.user_info_id)
    if user is None:
        sess.delete(row)
        sess.commit()
        return None

    # The token proves control of the address it was *issued* for. If the
    # account's address has changed since, that proof says nothing about the
    # new one.
    if normalize_email(user.email) != row.email:
        sess.delete(row)
        sess.commit()
        return None

    user.email_verified = True
    sess.add(user)
    sess.delete(row)
    sess.commit()
    sess.refresh(user)
    return user


async def send_verification_email(
    to_email: str, display_name: str, token: str
) -> None:
    """Background task: deliver the verification link.

    Failures are logged, never raised — this runs after the response has gone
    out, so there is no request left to report to, and an unhandled exception
    would just be a stray traceback. Same rationale as ``send_invite_email``
    in ``api/members.py``.
    """
    verify_url = f"{_frontend_origin()}/verify-email/{token}"
    text_body, html_body = render_verification_email(
        display_name=display_name,
        verify_url=verify_url,
        expires_hours=TOKEN_TTL_HOURS,
    )
    try:
        await get_email_service().send(EmailMessage(
            to=to_email,
            subject=f"Confirm your {APP_NAME} email address",
            text_body=text_body,
            html_body=html_body,
        ))
    except Exception:
        _logger.exception("Failed to send verification email to %s", to_email)
