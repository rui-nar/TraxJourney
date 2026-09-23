"""REST travel-companion endpoints — project members and invite links
(issue #106; roles: issue #109; invite emails: issue #113).

Routes:
    POST   /api/projects/{name}/members/invite     — create (or return) the invite link token,
                                                       optionally emailing it
    DELETE /api/projects/{name}/members/invite     — revoke the invite link
    GET    /api/projects/{name}/members            — list members (owner first)
    DELETE /api/projects/{name}/members/{user_id}  — remove a member; a member removes themself
    GET    /api/projects/{name}/members/pending    — list invites emailed but not yet accepted
    DELETE /api/projects/{name}/members/pending/{id} — revoke one pending invite
    GET    /api/invites/pending                    — invites addressed to me
    GET    /api/invites/{token}                    — preview an invite (project + owner name + role)
    POST   /api/invites/{token}/accept             — join the project with the invite's role
    DELETE /api/invites/{token}                    — decline an invite addressed to me

Member management (invite create/revoke, removing editors/viewers) is
co-owner+. Only the strict owner may create a "co-owner" invite or remove a
co-owner (so co-owners can't lock each other out). Any member may leave.
Invites are blocked while the owner has E2EE enabled — companions could
neither read nor write content encrypted under the owner's CMK (see plan for
issue #106; key sharing is a follow-up).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Annotated, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from models.db import get_session
from pydantic import BaseModel, Field
from sqlmodel import select

from api.deps import get_current_user
from api.geo import bust_project_cache
from api.project_access import require_role, resolve_project
from models.project_db import (
    DBProject,
    DBProjectInvite,
    DBProjectMember,
    DBProjectPendingInvite,
)
from models.user import UserInfo
from src.brand import APP_NAME
from src.email.address import is_valid_email, normalize_email
from src.email.service import EmailMessage, get_email_service
from src.email.templates import render_invite_email
from src.utils.rate_limit import consume_rate_limit

_logger = logging.getLogger(__name__)

# Same var/default as api/strava.py's OAuth-callback redirects — the base URL
# for links the server hands back to the browser.
_FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5500")

# Ceiling on invites awaiting acceptance for one trip (issue #110). Not a
# security boundary — the per-user send rate limit is — but it stops a
# runaway script or a typo'd loop from filling the table for one project.
_MAX_PENDING_PER_PROJECT = 50

router = APIRouter(prefix="/api/projects", tags=["members"])
invites_router = APIRouter(prefix="/api/invites", tags=["members"])

InviteRole = Literal["viewer", "editor", "co-owner"]


# ── Request / response schemas ──────────────────────────────────────────────

class InviteCreateBody(BaseModel):
    role: InviteRole = Field(
        default="editor",
        description='Role granted on accept. Only the trip owner (not a '
                    'co-owner) may create a "co-owner" invite.',
    )
    email: Optional[str] = Field(
        default=None,
        description="If set, email the join link to this address (issue #113). "
                    "Sending happens in the background — this doesn't block or "
                    "fail the request, and doesn't change the response.",
    )

class InviteTokenOut(BaseModel):
    token: str = Field(description="Invite token; append to /join/{token} to build the link")
    role: str = Field(description="Role this invite grants on accept")

class MemberOut(BaseModel):
    user_id: int = Field(description="Member's user id")
    display_name: str = Field(description="Member's display name")
    avatar_url: str = Field(description="Member's avatar URL, may be empty")
    role: str = Field(description='"owner", "co-owner", "editor", or "viewer"')

class MembersOut(BaseModel):
    members: List[MemberOut] = Field(description="Project members, owner first")

class InvitePreviewOut(BaseModel):
    project_name: str = Field(description="Name of the project the invite joins")
    owner_name: str = Field(description="Display name of the project owner")
    role: str = Field(description="Role this invite grants on accept")
    invited_email: Optional[str] = Field(
        default=None,
        description="For a targeted invite (issue #110), the address it was "
                    "sent to; null for the shared link, which anyone may accept.",
    )

class PendingInviteOut(BaseModel):
    id: int = Field(description="Pending invite id; pass to the revoke route")
    email: str = Field(description="Address the invite was sent to")
    role: str = Field(description="Role it grants on accept")
    created_at: float = Field(description="Unix timestamp the invite was created")

class PendingInvitesOut(BaseModel):
    invites: List[PendingInviteOut] = Field(description="Oldest first")

class MyPendingInviteOut(BaseModel):
    token: str = Field(description="Accept/decline token for this invite")
    project_name: str = Field(description="Trip the invite joins")
    owner_name: str = Field(description="Display name of the trip owner")
    role: str = Field(description="Role it grants on accept")
    created_at: float = Field(description="Unix timestamp the invite was created")

class MyPendingInvitesOut(BaseModel):
    invites: List[MyPendingInviteOut] = Field(description="Oldest first")

class InviteAcceptedOut(BaseModel):
    name: str = Field(description="Name of the joined project")
    owner_id: int = Field(description="User id of the project owner — pass as ?owner= on project routes")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _display_name(user: Optional[UserInfo]) -> str:
    if user is None:
        return ""
    return user.display_name or user.email


async def send_invite_email(to_email: str, project_name: str, owner_name: str,
                             role: str, token: str) -> None:
    """Background task (issue #113): render and send the invite email.

    Runs after the response has gone out (queued via BackgroundTasks), so a
    slow or misconfigured relay never delays or fails the invite-create
    request. There's no request left to report to, so a send failure (bad
    credentials, relay down, ...) is logged here rather than raised — an
    unhandled exception in a background task would otherwise just become a
    stray traceback in the server log with no other effect.
    """
    join_url = f"{_FRONTEND_ORIGIN}/join/{token}"
    text_body, html_body = render_invite_email(
        project_name=project_name, owner_name=owner_name, role=role, join_url=join_url)
    try:
        await get_email_service().send(EmailMessage(
            to=to_email,
            subject=f"{owner_name} invited you to {project_name} on {APP_NAME}",
            text_body=text_body,
            html_body=html_body,
        ))
    except Exception:
        _logger.exception("Failed to send invite email to %s", to_email)


def _get_invite(sess, token: str) -> tuple[DBProjectInvite, DBProject]:
    invite = sess.exec(
        select(DBProjectInvite).where(DBProjectInvite.token == token)
    ).first()
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    project = sess.get(DBProject, invite.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    return invite, project


def _resolve_any_invite(sess, token: str):
    """Look *token* up as either kind of invite (issue #110).

    Returns ``(row, project, is_pending)``. A shared link (``DBProjectInvite``)
    is one multi-use token per project that anyone signed in may accept; a
    pending invite (``DBProjectPendingInvite``) is addressed to one email and
    only that address may accept it. The join screen and the accept endpoint
    take either, so the recipient's experience is the same whichever kind of
    link they were sent.
    """
    pending = sess.exec(
        select(DBProjectPendingInvite).where(DBProjectPendingInvite.token == token)
    ).first()
    if pending is not None:
        project = sess.get(DBProject, pending.project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Invite not found")
        return pending, project, True

    invite, project = _get_invite(sess, token)
    return invite, project, False


def _verified_email(sess, user_info_id: int) -> str:
    """The caller's confirmed address, or "" when unverified/absent.

    Pending invites match on this rather than on ``UserInfo.email`` directly:
    an unconfirmed address is only a claim, and matching on a claim is exactly
    how someone else's invite gets stolen.
    """
    user = sess.get(UserInfo, user_info_id)
    if user is None or not user.email_verified:
        return ""
    return normalize_email(user.email)


# ── Member management (owner + members) ───────────────────────────────────────

@router.post("/{name}/members/invite", response_model=InviteTokenOut,
             summary="Create invite link")
def create_invite(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    body: Optional[InviteCreateBody] = None,
    owner: Optional[int] = None,
):
    """Create the project's invite token (idempotent — returns the existing
    one, unchanged, if one already exists; the requested role is ignored on
    that path). If ``body.email`` is set, also queues the join link to be
    emailed to that address (issue #113) — this happens regardless of
    whether the invite was just created or already existed, so "email the
    link to someone new" is just calling this again with their address."""
    user_info_id = int(current_user["sub"])
    invite_body = body or InviteCreateBody()
    role = invite_body.role
    # Normalise before validating: the owner typing a trailing space (or
    # pasting a capitalised address) is not a malformed address, and the
    # stored form has to be the normalised one anyway for matching to work.
    email = normalize_email(invite_body.email) if invite_body.email is not None else None
    if email is not None and not is_valid_email(email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Not a valid email address",
        )
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        if role == "co-owner":
            require_role(sess, row, user_info_id, "owner")
        owner_user = sess.get(UserInfo, row.user_info_id)
        if owner_user is not None and owner_user.encryption_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Travel companions are not available on encrypted accounts — "
                       "companions could not read or write encrypted trip content.",
            )
        invite = sess.exec(
            select(DBProjectInvite).where(DBProjectInvite.project_id == row.id)
        ).first()
        if invite is None:
            invite = DBProjectInvite(project_id=row.id, created_by=user_info_id, role=role)
            sess.add(invite)
            sess.commit()
            sess.refresh(invite)

        if email is None:
            return {"token": invite.token, "role": invite.role}

        # Emailing an address creates a *pending* invite with its own token
        # (issue #110) rather than mailing the shared link, so it can be
        # revoked individually and matched to the recipient's account.
        sender = sess.get(UserInfo, user_info_id)
        if sender is None or not sender.email_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Confirm your own email address before sending invites.",
            )
        if not consume_rate_limit(f"invite-send:{user_info_id}"):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many invites sent recently. "
                       "Please wait a few minutes and try again.",
            )

        target = email  # already normalised above
        pending = sess.exec(
            select(DBProjectPendingInvite).where(
                DBProjectPendingInvite.project_id == row.id,
                DBProjectPendingInvite.email == target,
            )
        ).first()
        if pending is None:
            outstanding = len(sess.exec(
                select(DBProjectPendingInvite).where(
                    DBProjectPendingInvite.project_id == row.id,
                    DBProjectPendingInvite.accepted_at == None,  # noqa: E711
                )
            ).all())
            if outstanding >= _MAX_PENDING_PER_PROJECT:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"This trip already has {_MAX_PENDING_PER_PROJECT} "
                           "invites waiting to be accepted. Revoke some first.",
                )
            pending = DBProjectPendingInvite(
                project_id=row.id, email=target, role=role,
                invited_by=user_info_id)
            sess.add(pending)
            sess.commit()
            sess.refresh(pending)

        background_tasks.add_task(
            send_invite_email, target, row.name, _display_name(owner_user),
            pending.role, pending.token)
        # The shared link's token is still what's returned: the caller asked to
        # email someone, not to be handed that person's private join token.
        return {"token": invite.token, "role": invite.role}


@router.delete("/{name}/members/invite", status_code=status.HTTP_204_NO_CONTENT,
               summary="Revoke invite link")
def revoke_invite(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: Optional[int] = None,
):
    """Delete the project's invite token. Existing members are unaffected."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        for invite in sess.exec(
            select(DBProjectInvite).where(DBProjectInvite.project_id == row.id)
        ).all():
            sess.delete(invite)
        sess.commit()


@router.get("/{name}/members/pending", response_model=PendingInvitesOut,
            summary="List pending email invites")
def list_pending_invites(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: Optional[int] = None,
):
    """Invites emailed to an address that hasn't joined yet (issue #110).
    Co-owner+, matching who may create and revoke them."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        rows = sess.exec(
            select(DBProjectPendingInvite)
            .where(
                DBProjectPendingInvite.project_id == row.id,
                DBProjectPendingInvite.accepted_at == None,  # noqa: E711
            )
            .order_by(DBProjectPendingInvite.created_at)
        ).all()
        return {"invites": [{
            "id": p.id,
            "email": p.email,
            "role": p.role,
            "created_at": p.created_at,
        } for p in rows]}


@router.delete("/{name}/members/pending/{invite_id}",
               status_code=status.HTTP_204_NO_CONTENT,
               summary="Revoke a pending email invite")
def revoke_pending_invite(
    name: str,
    invite_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: Optional[int] = None,
):
    """Delete one pending invite; its token stops working immediately. Unlike
    revoking the shared link, this cuts off exactly one person."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        pending = sess.get(DBProjectPendingInvite, invite_id)
        # The project check matters: without it, any co-owner of any trip could
        # revoke an invite belonging to a trip they have nothing to do with.
        if pending is None or pending.project_id != row.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Invite not found")
        sess.delete(pending)
        sess.commit()


@router.get("/{name}/members", response_model=MembersOut, summary="List members")
def list_members(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: Optional[int] = None,
):
    """Return the owner plus all members (any role). Visible to any member,
    including viewers."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        owner_user = sess.get(UserInfo, row.user_info_id)
        members: list[dict] = [{
            "user_id": row.user_info_id,
            "display_name": _display_name(owner_user),
            "avatar_url": owner_user.avatar_url if owner_user else "",
            "role": "owner",
        }]
        for m in sess.exec(
            select(DBProjectMember)
            .where(DBProjectMember.project_id == row.id)
            .order_by(DBProjectMember.created_at)
        ).all():
            u = sess.get(UserInfo, m.user_info_id)
            members.append({
                "user_id": m.user_info_id,
                "display_name": _display_name(u),
                "avatar_url": u.avatar_url if u else "",
                "role": m.role,
            })
        return {"members": members}


@router.delete("/{name}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove a member")
def remove_member(
    name: str,
    user_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: Optional[int] = None,
):
    """Co-owner+ removes an editor/viewer; a member may remove only themself
    (leave), at any role. Removing a co-owner is strict-owner-only, so
    co-owners can't lock each other out."""
    user_info_id = int(current_user["sub"])
    is_self = user_id == user_info_id
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner,
                              min_role="viewer" if is_self else "co-owner")
        member = sess.exec(
            select(DBProjectMember).where(
                DBProjectMember.project_id == row.id,
                DBProjectMember.user_info_id == user_id,
            )
        ).first()
        if member is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
        if not is_self and member.role == "co-owner":
            require_role(sess, row, user_info_id, "owner")
        owner_id = row.user_info_id  # read before the commit expires the row
        sess.delete(member)
        sess.commit()
    # /meta is cached per caller and carries their caller_role (issue #178), so
    # a membership change has to invalidate the project's payloads even though
    # no project data moved.
    bust_project_cache(owner_id, name)


# ── Invite preview / accept (any authenticated user) ──────────────────────────

@invites_router.get("/pending", response_model=MyPendingInvitesOut,
                    summary="List invites addressed to me")
def list_my_pending_invites(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Unaccepted invites sent to the caller's confirmed address (issue #110).

    Empty for an unverified account — matching on an unconfirmed address is
    exactly how someone else's invite would get stolen.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        email = _verified_email(sess, user_info_id)
        if not email:
            return {"invites": []}
        rows = sess.exec(
            select(DBProjectPendingInvite)
            .where(
                DBProjectPendingInvite.email == email,
                DBProjectPendingInvite.accepted_at == None,  # noqa: E711
            )
            .order_by(DBProjectPendingInvite.created_at)
        ).all()
        out = []
        for p in rows:
            project = sess.get(DBProject, p.project_id)
            if project is None or project.user_info_id == user_info_id:
                continue
            out.append({
                "token": p.token,
                "project_name": project.name,
                "owner_name": _display_name(sess.get(UserInfo, project.user_info_id)),
                "role": p.role,
                "created_at": p.created_at,
            })
        return {"invites": out}


@invites_router.get("/{token}", response_model=InvitePreviewOut, summary="Preview an invite")
def preview_invite(
    token: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    with get_session() as sess:
        invite, project, is_pending = _resolve_any_invite(sess, token)
        return {
            "project_name": project.name,
            "owner_name": _display_name(sess.get(UserInfo, project.user_info_id)),
            "role": invite.role,
            # Set only for a targeted invite, so the join screen can say up
            # front who it's for instead of letting the user tap Join and
            # then explaining the 403.
            "invited_email": invite.email if is_pending else None,
        }


@invites_router.delete("/{token}", status_code=status.HTTP_204_NO_CONTENT,
                       summary="Decline a pending invite")
def decline_invite(
    token: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Decline an invite addressed to the caller (issue #110).

    Only pending invites can be declined — the shared link has no per-person
    state to decline, and deleting it would revoke everyone's access on behalf
    of an owner who never asked.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        invite, _project, is_pending = _resolve_any_invite(sess, token)
        if not is_pending:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This kind of invite link can't be declined.",
            )
        if invite.email != _verified_email(sess, user_info_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Invite not found")
        sess.delete(invite)
        sess.commit()


@invites_router.post("/{token}/accept", response_model=InviteAcceptedOut,
                     summary="Accept an invite")
def accept_invite(
    token: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Join the invite's project with its role. Idempotent for existing
    members (role is not changed by re-accepting).

    A shared link may be accepted by anyone signed in. A pending invite
    (issue #110) may only be accepted from the confirmed address it was sent
    to — otherwise a forwarded link would hand membership to whoever received
    it, which is the whole point of addressing an invite in the first place.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        invite, project, is_pending = _resolve_any_invite(sess, token)
        if project.user_info_id == user_info_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You already own this trip",
            )
        if is_pending and invite.email != _verified_email(sess, user_info_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This invite was sent to {invite.email}. Sign in with "
                       "that address and confirm it to accept.",
            )
        existing = sess.exec(
            select(DBProjectMember).where(
                DBProjectMember.project_id == project.id,
                DBProjectMember.user_info_id == user_info_id,
            )
        ).first()
        if existing is None:
            sess.add(DBProjectMember(
                project_id=project.id,
                user_info_id=user_info_id,
                role=invite.role,
                invited_by=invite.invited_by if is_pending else invite.created_by,
            ))
        if is_pending and invite.accepted_at is None:
            # Kept rather than deleted so the owner's list can show the invite
            # was taken up, and so re-accepting is idempotent instead of a 404.
            invite.accepted_at = time.time()
            sess.add(invite)
        project_name, project_owner = project.name, project.user_info_id
        sess.commit()
    # See remove_member: the new member's caller_role has to be built fresh.
    bust_project_cache(project_owner, project_name)
    return {"name": project_name, "owner_id": project_owner}
