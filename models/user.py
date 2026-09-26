"""User-related database models."""
from __future__ import annotations

import time
import uuid
from typing import Optional

import bcrypt
import sqlmodel


class LocalUser(sqlmodel.SQLModel, table=True):
    """Stores credentials for local (username + password) accounts."""

    __tablename__ = "localuser"

    id: int | None = sqlmodel.Field(default=None, primary_key=True)
    username: str = sqlmodel.Field(default="", unique=True, index=True)
    password_hash: bytes = sqlmodel.Field(default=b"")
    enabled: bool = sqlmodel.Field(default=True)
    # Forces a password change on next login (seeded admin, admin-reset accounts).
    password_change_required: bool = sqlmodel.Field(default=False)

    @staticmethod
    def hash_password(password: str) -> bytes:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt())

    def verify(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return bcrypt.checkpw(password.encode(), self.password_hash)


class UserInfo(sqlmodel.SQLModel, table=True):
    """Stores profile data for each registered user."""

    # AUTOINCREMENT: SQLite would otherwise hand a deleted account's id to the
    # next one registered, and that id lives on outside this table — in Stripe
    # metadata above all, where a late event would then name a stranger
    # (issue #429). No effect on Postgres, whose sequences never reuse.
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = sqlmodel.Field(default=None, primary_key=True)
    local_auth_id: int | None = sqlmodel.Field(
        default=None, foreign_key="localuser.id"
    )
    google_sub: str = sqlmodel.Field(default="")
    display_name: str = sqlmodel.Field(default="")
    email: str = sqlmodel.Field(default="", index=True)
    avatar_url: str = sqlmodel.Field(default="")
    auth_provider: str = sqlmodel.Field(default="local")  # "local" | "google"
    is_admin: bool = sqlmodel.Field(default=False)
    # Email verification (issue #110). Soft gate: an unverified account is
    # fully usable, but may not send invite emails and is not matched to a
    # pending invite — otherwise anyone could register with someone else's
    # address and claim an invite meant for them.
    email_verified: bool = sqlmodel.Field(default=False)
    created_at: float = sqlmodel.Field(default_factory=time.time)
    # E2EE (issue #26): True once the user has enabled client-side encryption.
    # The server still holds no keys; this only signals clients to expect
    # ciphertext for in-scope fields. Backed by device_key / recovery_wrap rows.
    encryption_enabled: bool = sqlmodel.Field(default=False)


class StravaToken(sqlmodel.SQLModel, table=True):
    """Stores per-user Strava OAuth tokens."""

    id: Optional[int] = sqlmodel.Field(default=None, primary_key=True)
    user_info_id: int = sqlmodel.Field(
        foreign_key="userinfo.id", unique=True, index=True
    )
    access_token: str = sqlmodel.Field(default="")
    refresh_token: str = sqlmodel.Field(default="")
    expires_at: float = sqlmodel.Field(default=0.0)


class PolarstepsToken(sqlmodel.SQLModel, table=True):
    """Stores per-user Polarsteps remember_token (unofficial API cookie)."""

    id: Optional[int] = sqlmodel.Field(default=None, primary_key=True)
    user_info_id: int = sqlmodel.Field(
        foreign_key="userinfo.id", unique=True, index=True
    )
    remember_token: str = sqlmodel.Field(default="")
    polarsteps_user_id: int = sqlmodel.Field(default=0)
    polarsteps_username: str = sqlmodel.Field(default="")


class ImmichToken(sqlmodel.SQLModel, table=True):
    """Stores per-user Immich server URL + API key (self-hosted photo library)."""

    id: Optional[int] = sqlmodel.Field(default=None, primary_key=True)
    user_info_id: int = sqlmodel.Field(
        foreign_key="userinfo.id", unique=True, index=True
    )
    server_url: str = sqlmodel.Field(default="")
    api_key: str = sqlmodel.Field(default="")


class EmailVerification(sqlmodel.SQLModel, table=True):
    """A live email-verification token (issue #110).

    At most one row per user (``user_info_id`` is unique) — requesting a resend
    replaces the previous token rather than accumulating valid ones, so a
    forwarded or leaked older link stops working.

    ``email`` records the address the token was issued for. Verifying only
    counts if the account still carries that address; without this, a token
    issued for one address could be used to mark a later, different address
    verified.
    """

    __tablename__ = "emailverification"

    id: Optional[int] = sqlmodel.Field(default=None, primary_key=True)
    user_info_id: int = sqlmodel.Field(
        foreign_key="userinfo.id", unique=True, index=True
    )
    token: str = sqlmodel.Field(
        default_factory=lambda: uuid.uuid4().hex, index=True, unique=True)
    email: str = sqlmodel.Field(default="")
    created_at: float = sqlmodel.Field(default_factory=time.time)
    expires_at: float = sqlmodel.Field(default=0.0)
