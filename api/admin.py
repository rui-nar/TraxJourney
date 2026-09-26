"""Admin dashboard REST endpoints (issue #25).

All routes require an admin caller (``require_admin`` re-reads ``is_admin`` from
the DB). Nothing here ever returns memory/journal *content* — only counts,
sizes, and profile fields — so the dashboard cannot leak private data.

Aggregates are computed with GROUP BY (no N+1). Storage is read from the TTL
cache in ``src.admin.storage`` and the filesystem walk happens OUTSIDE the DB
session so a slow walk never pins a pooled connection.
"""
from __future__ import annotations

import html
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import select

from api.deps import require_admin
from models.billing import Subscription
from models.db import get_session
from models.project_db import DBActivity, DBMemory, DBProject
from models.user import LocalUser, UserInfo
from src.admin.storage import cached_user_storage, refresh_storage_cache
from src.admin.tiers import user_encryption_tier
from src.billing.entitlements import plan_display_name, plan_from_subscription
from src.billing.plans import FREE, PLAN_ORDER
from src.billing.subscriptions import set_admin_override
from src.email.service import EmailMessage, get_email_service
from src.utils.logging import (
    LEVEL_NAMES,
    current_level_info,
    get_logger,
    publish_level_override,
    revert_log_level_override,
)

_log = get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Window for the "recent sign-ups" headline metric.
_RECENT_SIGNUP_WINDOW_SECONDS = 7 * 24 * 3600

# Length (bytes) of the random reset password; hex-encoded → 32 chars.
_RESET_PASSWORD_BYTES = 16

# Tiers for which an admin password reset is permitted. Medium/High are
# zero-knowledge enough that a server-side reset would silently destroy the
# user's encrypted data, so they are hard-blocked.
_RESETTABLE_TIERS = frozenset({"none", "low"})

# Longest a live log-level override may run before an admin has to re-apply it
# (issue #208). Bounds the worst case of a forgotten DEBUG override quietly
# burning log volume in production for weeks.
_MAX_LOG_LEVEL_OVERRIDE_MINUTES = 24 * 60

# APScheduler job id for the auto-revert — fixed and reused (replace_existing)
# so a second PUT reschedules rather than stacking multiple pending reverts.
_LOG_LEVEL_REVERT_JOB_ID = "log_level_revert"


# ── Response schemas ──────────────────────────────────────────────────────────

class UserRow(BaseModel):
    id: int
    email: str
    display_name: str
    auth_provider: str
    created_at: float
    project_count: int
    activity_count: int
    memory_count: int
    storage_bytes: int
    encryption_tier: str
    is_admin: bool
    plan: str
    plan_name: str
    is_comped: bool
    subscription_status: str
    stripe_customer_url: str | None


class Totals(BaseModel):
    users: int
    projects: int
    activities: int
    memories: int
    storage_bytes: int
    recent_signups_7d: int


class StatsResponse(BaseModel):
    totals: Totals
    users: list[UserRow]


class SearchResult(BaseModel):
    id: int
    email: str
    username: str
    display_name: str
    auth_provider: str
    encryption_tier: str
    is_admin: bool
    plan: str
    plan_name: str
    is_comped: bool
    subscription_status: str
    stripe_customer_url: str | None


class ResetPasswordResponse(BaseModel):
    temp_password: str = Field(description="Shown once; not recoverable afterwards")


class SetAdminRequest(BaseModel):
    is_admin: bool = Field(description="True to grant admin access, False to revoke it")


class OkResponse(BaseModel):
    ok: bool = True


class SetPlanRequest(BaseModel):
    plan: str = Field(
        description='Plan to grant regardless of payment — "free", "tier_1", '
                    '"tier_2", "tier_3", or "" to clear the override and fall '
                    'back to what the payment provider says.'
    )


class BroadcastEmailRequest(BaseModel):
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1, description="Plain text; sent as-is, also wrapped as simple HTML")
    user_ids: list[int] = Field(
        default_factory=list,
        description="Recipient UserInfo ids; ignored when send_to_all is true",
    )
    send_to_all: bool = False


class BroadcastEmailResponse(BaseModel):
    sent_count: int


class LogLevelResponse(BaseModel):
    effective_level: str = Field(description="The level actually in effect right now")
    source: str = Field(description='"env" (LOG_LEVEL / default) or "override" (live, admin-set)')
    env_level: str = Field(description="The restart-persistent baseline (LOG_LEVEL, default INFO)")
    override_level: str | None = Field(description="The live override's level, if one is active")
    override_expires_at: float | None = Field(
        description="Unix timestamp the override auto-reverts at, or null for an indefinite override"
    )


class SetLogLevelRequest(BaseModel):
    level: str = Field(description='One of "DEBUG", "INFO", "WARNING", "ERROR"')
    duration_minutes: int | None = Field(
        default=None,
        description="Auto-revert after this many minutes; null applies indefinitely "
                    "(until manually reverted or the process restarts)",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _counts_by_user(sess, col, model) -> dict[int, int]:
    """GROUP BY aggregate: {user_info_id: row_count} for a per-user table."""
    return {
        uid: cnt
        for uid, cnt in sess.exec(
            select(col, func.count()).group_by(col)
        ).all()
    }


def _stripe_customer_url(customer_id: str) -> str | None:
    """Stripe dashboard link for a customer id, or ``None`` with no customer to
    link to (a comped-only account never checked out).

    Test-mode and live accounts have different dashboard hosts; which one we're
    on is read off ``STRIPE_SECRET_KEY`` the same way scripts/stripe_catalog.py
    tells a live key from a test one.
    """
    if not customer_id:
        return None
    is_live = os.environ.get("STRIPE_SECRET_KEY", "").strip().startswith("sk_live_")
    base = "https://dashboard.stripe.com/customers/" if is_live \
        else "https://dashboard.stripe.com/test/customers/"
    return base + customer_id


def _billing_fields(sub: Subscription | None) -> dict:
    """Plan/comp/Stripe-link fields for one user's ``UserRow``/``SearchResult``.

    ``sub`` is ``None`` for a user who never checked out and was never comped —
    plain ``free``, same as a subscription row with no plan and no override.
    """
    if sub is None:
        return {
            "plan": FREE,
            "plan_name": plan_display_name(FREE),
            "is_comped": False,
            "subscription_status": "none",
            "stripe_customer_url": None,
        }
    plan = sub.admin_override_plan or plan_from_subscription(
        sub.plan, sub.status, sub.current_period_end
    )
    return {
        "plan": plan,
        "plan_name": plan_display_name(plan),
        "is_comped": bool(sub.admin_override_plan),
        "subscription_status": sub.status or "none",
        "stripe_customer_url": _stripe_customer_url(sub.provider_customer_id),
    }


def _log_level_response() -> LogLevelResponse:
    info = current_level_info()
    return LogLevelResponse(
        effective_level=logging.getLevelName(info.effective_level),
        source=info.source,
        env_level=logging.getLevelName(info.env_level),
        override_level=(
            logging.getLevelName(info.override_level)
            if info.override_level is not None else None
        ),
        override_expires_at=info.override_expires_at,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse, summary="Dashboard metrics")
def stats(_admin: Annotated[dict, Depends(require_admin)]):
    """Totals + per-user breakdown. No memory/journal content is returned."""
    now = time.time()
    with get_session() as sess:
        users = sess.exec(select(UserInfo)).all()

        project_counts = _counts_by_user(sess, DBProject.user_info_id, DBProject)
        activity_counts = _counts_by_user(sess, DBActivity.user_info_id, DBActivity)

        # Memories belong to a project which belongs to a user: join + group by owner.
        memory_counts = {
            uid: cnt
            for uid, cnt in sess.exec(
                select(DBProject.user_info_id, func.count(DBMemory.id))
                .join(DBMemory, DBMemory.project_id == DBProject.id)
                .group_by(DBProject.user_info_id)
            ).all()
        }

        tiers = {u.id: user_encryption_tier(sess, u.id) for u in users}
        # One bulk fetch for every user's subscription row, same reasoning as
        # the GROUP BY counts above: N+1 here would mean one query per user.
        subs_by_user = {
            s.user_info_id: s for s in sess.exec(select(Subscription)).all()
        }
        # Snapshot the plain profile fields before leaving the session so the
        # (potentially slow) storage walk below holds no DB connection.
        profiles = [
            (u.id, u.email, u.display_name, u.auth_provider, u.created_at, bool(u.is_admin))
            for u in users
        ]

    # Storage walk happens OUTSIDE the session (see src.admin.storage).
    rows: list[UserRow] = []
    total_storage = 0
    for uid, email, display_name, auth_provider, created_at, is_admin in profiles:
        storage = cached_user_storage(str(uid), now=now)
        total_storage += storage
        rows.append(UserRow(
            id=uid,
            email=email,
            display_name=display_name,
            auth_provider=auth_provider,
            created_at=created_at,
            project_count=project_counts.get(uid, 0),
            activity_count=activity_counts.get(uid, 0),
            memory_count=memory_counts.get(uid, 0),
            storage_bytes=storage,
            encryption_tier=tiers.get(uid, "none"),
            is_admin=is_admin,
            **_billing_fields(subs_by_user.get(uid)),
        ))

    recent = sum(
        1 for _, _, _, _, created_at, _ in profiles
        if created_at and (now - created_at) < _RECENT_SIGNUP_WINDOW_SECONDS
    )

    return StatsResponse(
        totals=Totals(
            users=len(rows),
            projects=sum(project_counts.values()),
            activities=sum(activity_counts.values()),
            memories=sum(memory_counts.values()),
            storage_bytes=total_storage,
            recent_signups_7d=recent,
        ),
        users=rows,
    )


@router.get("/storage/refresh", summary="Force-recompute storage cache")
def storage_refresh(_admin: Annotated[dict, Depends(require_admin)]):
    """Bust the storage TTL cache so the next /stats re-walks the filesystem."""
    refresh_storage_cache()
    return {"ok": True}


@router.get("/users/search", response_model=list[SearchResult],
            summary="Search users by email / username / display name")
def search_users(
    _admin: Annotated[dict, Depends(require_admin)],
    q: str = "",
    limit: int = 50,
):
    """Case-insensitive LIKE across email, username, display_name."""
    q = q.strip()
    if not q:
        return []
    like = f"%{q.lower()}%"
    with get_session() as sess:
        rows = sess.exec(
            select(UserInfo, LocalUser, Subscription)
            .join(LocalUser, LocalUser.id == UserInfo.local_auth_id, isouter=True)
            .join(Subscription, Subscription.user_info_id == UserInfo.id, isouter=True)
            .where(
                func.lower(UserInfo.email).like(like)
                | func.lower(UserInfo.display_name).like(like)
                | func.lower(func.coalesce(LocalUser.username, "")).like(like)
            )
            .limit(limit)
        ).all()
        return [
            SearchResult(
                id=ui.id,
                email=ui.email,
                username=lu.username if lu else "",
                display_name=ui.display_name,
                auth_provider=ui.auth_provider,
                encryption_tier=user_encryption_tier(sess, ui.id),
                is_admin=bool(ui.is_admin),
                **_billing_fields(sub),
            )
            for ui, lu, sub in rows
        ]


@router.post("/users/{user_info_id}/set-admin", response_model=OkResponse,
             summary="Grant or revoke admin access for a user")
def set_admin(
    user_info_id: int,
    body: SetAdminRequest,
    admin: Annotated[dict, Depends(require_admin)],
):
    """Toggle ``is_admin`` for a user. An admin cannot revoke their own access,
    so there's always at least one admin left who can undo a mistake."""
    if not body.is_admin and str(user_info_id) == str(admin.get("sub")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot remove your own admin access.",
        )
    with get_session() as sess:
        user_info = sess.get(UserInfo, user_info_id)
        if user_info is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        user_info.is_admin = body.is_admin
        sess.add(user_info)
        sess.commit()

    _log.info(
        "Admin set is_admin=%s for user_info_id=%s", body.is_admin, user_info_id
    )
    return {"ok": True}


@router.put("/users/{user_info_id}/plan", response_model=OkResponse,
            summary="Grant or clear a plan override for a user")
def set_plan(
    user_info_id: int,
    body: SetPlanRequest,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Comp an account, or undo a comp.

    The override is stored beside the provider state, never on top of it, so a
    later webhook cannot silently wipe a comped plan — and clearing the override
    returns the user to whatever they are actually paying for.
    """
    plan = (body.plan or "").strip().lower()
    if plan and plan not in PLAN_ORDER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown plan '{plan}'",
        )
    with get_session() as sess:
        if sess.get(UserInfo, user_info_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        set_admin_override(sess, user_info_id, plan)

    _log.info("Admin set plan override '%s' for user_info_id=%s", plan, user_info_id)
    return {"ok": True}


@router.post("/users/{user_info_id}/reset-password",
             response_model=ResetPasswordResponse,
             summary="Reset a user's password (None/Low tiers only)")
def reset_password(
    user_info_id: int,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Set a random temp password + force change. Blocked for Medium/High tiers.

    Medium/High encryption is zero-knowledge enough that overwriting the server
    password would orphan the user's encrypted data, so those are refused (409).
    Google accounts have no server password → 409.
    """
    with get_session() as sess:
        user_info = sess.get(UserInfo, user_info_id)
        if user_info is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        tier = user_encryption_tier(sess, user_info_id)
        if tier not in _RESETTABLE_TIERS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Password reset is unavailable for this account: its "
                    f"encryption tier ({tier}) is zero-knowledge, so the server "
                    "cannot reset the password without destroying encrypted data."
                ),
            )

        local_user = sess.get(LocalUser, user_info.local_auth_id)
        if local_user is None or not local_user.password_hash:
            # Google (or otherwise passwordless) account — no server password.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account has no server-managed password to reset.",
            )

        temp_password = secrets.token_hex(_RESET_PASSWORD_BYTES)
        local_user.password_hash = LocalUser.hash_password(temp_password)
        local_user.password_change_required = True
        sess.add(local_user)
        sess.commit()

    _log.info(
        "Admin reset password for user_info_id=%s (tier=%s)", user_info_id, tier
    )
    return ResetPasswordResponse(temp_password=temp_password)


@router.delete("/users/{user_info_id}", response_model=OkResponse,
               summary="Permanently delete a user and all their data")
def delete_user(
    user_info_id: int,
    admin: Annotated[dict, Depends(require_admin)],
):
    """Irreversibly delete a user's account, projects, and every row/file they
    own. An admin cannot delete their own account this way — use account
    settings for that, so there's always an admin left to act."""
    if str(user_info_id) == str(admin.get("sub")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot delete your own account here.",
        )
    from src.auth.account_deletion import (
        BILLING_UNAVAILABLE,
        delete_user_and_data,
        purge_user_files,
    )
    from src.exceptions.errors import AccountDeletionRefused

    with get_session() as sess:
        if sess.get(UserInfo, user_info_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        try:
            delete_user_and_data(sess, user_info_id)
        except AccountDeletionRefused as exc:
            if exc.code != BILLING_UNAVAILABLE:
                raise
            # The operator is the one who can fix this, so they get the way
            # out; the user deleting themselves is only told to ask them.
            raise AccountDeletionRefused(
                "This user has a paid plan that this server cannot cancel, "
                "because billing is not configured. Nothing was deleted. To "
                "resolve it, follow \"Deleting an account\" in docs/BILLING.md.",
                status_code=exc.status_code, code=exc.code,
            ) from exc
    purge_user_files(user_info_id)

    _log.info("Admin deleted user_info_id=%s", user_info_id)
    return {"ok": True}


@router.get("/log-level", response_model=LogLevelResponse,
            summary="Current effective log level")
def get_log_level(_admin: Annotated[dict, Depends(require_admin)]):
    return _log_level_response()


@router.put("/log-level", response_model=LogLevelResponse,
            summary="Apply a live log-level override (no restart required)")
def set_log_level(
    body: SetLogLevelRequest,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Overrides the effective level on this process immediately, and
    publishes it to Redis (when configured) so the worker process picks it
    up before its next job (issue #208). Process-memory only: a restart of
    either process drops back to the ``LOG_LEVEL`` baseline, same as
    clearing the override via ``DELETE``.
    """
    level_name = body.level.strip().upper()
    if level_name not in LEVEL_NAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown level {body.level!r}; must be one of {sorted(LEVEL_NAMES)}",
        )
    if body.duration_minutes is not None and not (
        0 < body.duration_minutes <= _MAX_LOG_LEVEL_OVERRIDE_MINUTES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"duration_minutes must be between 1 and {_MAX_LOG_LEVEL_OVERRIDE_MINUTES}",
        )

    level = getattr(logging, level_name)
    expires_at = (
        time.time() + body.duration_minutes * 60
        if body.duration_minutes is not None else None
    )
    publish_level_override(level, expires_at)

    # Imported here, not at module load: api.router imports this module while
    # building itself, so importing api.router back at load time would be
    # circular. By request time api.router has long finished importing.
    from api.router import _scheduler

    if expires_at is not None:
        _scheduler.add_job(
            revert_log_level_override, "date",
            run_date=datetime.fromtimestamp(expires_at, tz=timezone.utc),
            id=_LOG_LEVEL_REVERT_JOB_ID, replace_existing=True,
        )
    elif _scheduler.get_job(_LOG_LEVEL_REVERT_JOB_ID) is not None:
        # An earlier finite-duration override left a pending revert behind;
        # this indefinite one supersedes it.
        _scheduler.remove_job(_LOG_LEVEL_REVERT_JOB_ID)

    _log.info(
        "Admin set log level override=%s duration_minutes=%s",
        level_name, body.duration_minutes,
    )
    return _log_level_response()


@router.delete("/log-level", response_model=LogLevelResponse,
               summary="Revert to the env-configured (LOG_LEVEL) baseline")
def clear_log_level(_admin: Annotated[dict, Depends(require_admin)]):
    revert_log_level_override()

    from api.router import _scheduler

    if _scheduler.get_job(_LOG_LEVEL_REVERT_JOB_ID) is not None:
        _scheduler.remove_job(_LOG_LEVEL_REVERT_JOB_ID)

    _log.info("Admin cleared log level override")
    return _log_level_response()


async def _send_broadcast_email(to_email: str, subject: str, text_body: str) -> None:
    """Background task: send one broadcast email. Logged, not raised, on
    failure — same rationale as ``send_invite_email`` in ``api/members.py``:
    there's no request left to report to once this runs."""
    html_body = f"<p>{html.escape(text_body).replace(chr(10), '<br>')}</p>"
    try:
        await get_email_service().send(EmailMessage(
            to=to_email, subject=subject, text_body=text_body, html_body=html_body,
        ))
    except Exception:
        _log.exception("Failed to send broadcast email to %s", to_email)


@router.post("/broadcast-email", response_model=BroadcastEmailResponse,
             summary="Send a plain-text email to selected users, or all users")
def broadcast_email(
    body: BroadcastEmailRequest,
    background_tasks: BackgroundTasks,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Queues one email per recipient via BackgroundTasks so a slow/broken
    relay never delays the response (mirrors the invite-email path)."""
    with get_session() as sess:
        if body.send_to_all:
            recipients = [u.email for u in sess.exec(select(UserInfo)).all() if u.email]
        else:
            if not body.user_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="No recipients selected",
                )
            recipients = [
                u.email for u in sess.exec(
                    select(UserInfo).where(UserInfo.id.in_(body.user_ids))
                ).all() if u.email
            ]

    if not recipients:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No recipients with an email address",
        )

    for to_email in recipients:
        background_tasks.add_task(_send_broadcast_email, to_email, body.subject, body.body)

    _log.info("Admin queued broadcast email to %d recipient(s)", len(recipients))
    return BroadcastEmailResponse(sent_count=len(recipients))
