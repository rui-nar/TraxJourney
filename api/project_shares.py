"""REST project sharing endpoints — share-link create/revoke, share info, visitors.

Routes:
    POST   /api/projects/{name}/share                    — create share link
    DELETE /api/projects/{name}/share                     — revoke share link
    PUT    /api/projects/{name}/share/content              — upload per-share encrypted memory content
    GET    /api/projects/{name}/share-info                — get share tokens
    POST   /api/projects/{name}/share/no-memories         — create no-memories share link
    DELETE /api/projects/{name}/share/no-memories         — revoke no-memories share link
    GET    /api/projects/{name}/share/visitors            — get share link visitor stats
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Annotated, Any, Dict, List, Optional

from models.db import get_session
from sqlmodel import select

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.deps import get_current_user, jwt_secret
from api.members import public_name_given_username
from api.memories import _utc_now
from api.project_access import OwnerParam, resolve_project
from models.project_db import DBMemory, DBShareMemoryContent, DBShareVisit
from models.user import LocalUser, UserInfo
from src.utils.encryption_check import is_encrypted_envelope

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Response schemas ──────────────────────────────────────────────────────────

class ShareTokenOut(BaseModel):
    share_token: str = Field(description="Public share token for full-project access")

class ShareTokenNoMemoriesOut(BaseModel):
    share_token_no_memories: str = Field(description="Public share token with memories stripped")

class ShareInfoOut(BaseModel):
    share_token: Optional[str] = Field(None, description="Full-access share token, or null if not created")
    share_token_no_memories: Optional[str] = Field(None, description="No-memories share token, or null if not created")

class ShareMemoryContentItem(BaseModel):
    memory_id: int
    name_ciphertext: Optional[str] = None
    description_ciphertext: Optional[str] = None

class ShareMemoryContentBody(BaseModel):
    items: List[ShareMemoryContentItem]

class ShareMemoryContentOut(BaseModel):
    updated: int = Field(description="Number of memories whose share-encrypted content was upserted")


# ── Project sharing ────────────────────────────────────────────────────────────

@router.post("/{name}/share", response_model=ShareTokenOut, summary="Create share link")
def create_share_link(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Generate (or return existing) share token for public read-only access."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        if not row.share_token:
            row.share_token = str(uuid.uuid4())
            sess.add(row)
            sess.commit()
        token = row.share_token
    return {"share_token": token}


@router.delete("/{name}/share", status_code=status.HTTP_204_NO_CONTENT)
def revoke_share_link(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Revoke the share token — the project becomes private again.

    Also deletes any DBShareMemoryContent rows for the "full" token type: the
    per-share content key lives only in that token's URL fragment, so once the
    token is gone the re-encrypted content is unusable and must not linger.
    No-op if there was nothing to delete (mirrors the token's own no-op case).
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        if row.share_token:
            from src.tile_renderer import invalidate_tile_cache
            from api.share import invalidate_share_cache
            invalidate_tile_cache(row.share_token)
            invalidate_share_cache(row.share_token)
            _delete_share_memory_content(sess, row.id, "full")
        row.share_token = None
        sess.add(row)
        sess.commit()


def _delete_share_memory_content(sess, project_id: int, token_type: str) -> None:
    """Delete all DBShareMemoryContent rows for this project's memories + token_type."""
    memory_ids = sess.exec(
        select(DBMemory.id).where(DBMemory.project_id == project_id)
    ).all()
    if not memory_ids:
        return
    rows = sess.exec(
        select(DBShareMemoryContent).where(
            DBShareMemoryContent.memory_id.in_(memory_ids),
            DBShareMemoryContent.token_type == token_type,
        )
    ).all()
    for row in rows:
        sess.delete(row)


@router.put("/{name}/share/content", response_model=ShareMemoryContentOut,
            summary="Upload per-share encrypted memory content")
def upload_share_memory_content(
    name: str,
    body: ShareMemoryContentBody,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Bulk-upsert re-encrypted memory content for the project's "full" share
    token (issue #28). This is a client-driven bulk upsert, not per-memory
    CRUD: the owner's client walks its already-decrypted memories, re-encrypts
    each actually-encrypted one under a fresh per-share content key, and PUTs
    every resulting envelope here in one call — calling again simply
    overwrites the previous envelopes (idempotent regeneration).

    Only memories that both belong to this project AND are themselves E2EE
    (name or description is a ciphertext envelope) are accepted; anything
    else in the payload is silently skipped rather than rejecting the batch.
    The share key used to produce these envelopes is never part of the
    request — the server only ever sees ciphertext.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")

        updated = 0
        now = _utc_now()
        for item in body.items:
            mem = sess.get(DBMemory, item.memory_id)
            if mem is None or mem.project_id != row.id:
                continue
            if not (is_encrypted_envelope(mem.name) or is_encrypted_envelope(mem.description)):
                continue
            existing = sess.exec(
                select(DBShareMemoryContent).where(
                    DBShareMemoryContent.memory_id == item.memory_id,
                    DBShareMemoryContent.token_type == "full",
                )
            ).first()
            if existing:
                existing.name_ciphertext = item.name_ciphertext
                existing.description_ciphertext = item.description_ciphertext
                sess.add(existing)
            else:
                sess.add(DBShareMemoryContent(
                    memory_id=item.memory_id,
                    token_type="full",
                    name_ciphertext=item.name_ciphertext,
                    description_ciphertext=item.description_ciphertext,
                    created_at=now,
                ))
            updated += 1
        sess.commit()

        if row.share_token:
            from api.share import invalidate_share_cache
            invalidate_share_cache(row.share_token)

    return {"updated": updated}


@router.get("/{name}/share-info", response_model=ShareInfoOut, summary="Get share tokens")
def get_share_info(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Return both share tokens for the project (null when not yet created)."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        return {
            "share_token": row.share_token,
            "share_token_no_memories": row.share_token_no_memories,
        }


@router.post("/{name}/share/no-memories", response_model=ShareTokenNoMemoriesOut,
             summary="Create no-memories share link")
def create_share_link_no_memories(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Create (idempotent) a share token that strips memory items."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        if not row.share_token_no_memories:
            row.share_token_no_memories = str(uuid.uuid4())
            sess.add(row)
            sess.commit()
        return {"share_token_no_memories": row.share_token_no_memories}


@router.delete("/{name}/share/no-memories", status_code=status.HTTP_204_NO_CONTENT,
               summary="Revoke no-memories share link")
def revoke_share_link_no_memories(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Revoke the no-memories share token."""
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="co-owner")
        if row.share_token_no_memories:
            from src.tile_renderer import invalidate_tile_cache
            from api.share import invalidate_share_cache
            invalidate_tile_cache(row.share_token_no_memories)
            invalidate_share_cache(row.share_token_no_memories)
        row.share_token_no_memories = None
        sess.add(row)
        sess.commit()


def _visitor_key(owner_id: int, visitor_id: int) -> str:
    """Stable pseudonymous id for a signed-in visitor, scoped to one trip owner
    (issue #431). The owner can tell two visitors apart across calls, but the
    key is not the account id and carries no email. The key itself is
    owner-scoped — the same visitor gets a different one on another owner's
    trips — though the display name and avatar sent alongside are the same
    everywhere, so this alone does not stop owners comparing notes. Keyed off
    the server secret with a domain-separated message so it never doubles as
    a token; rotating JWT_SECRET therefore relabels every visitor_key (nothing
    persists them, so that is harmless)."""
    msg = f"share-visitor:{owner_id}:{visitor_id}".encode()
    return hmac.new(jwt_secret().encode(), msg, hashlib.sha256).hexdigest()[:16]


@router.get("/{name}/share/visitors", summary="Get share link visitor stats")
def get_share_visitors(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Return visitor stats for both share link types.

    Response shape:
      {
        full: { anonymous_count: N,
                registered: [{visitor_key, display_name, avatar_url, last_seen_at}] },
        no_memories: { anonymous_count: N, registered: [...] }
      }

    Registered entries never carry the visitor's email or account id
    (issue #431): ``visitor_key`` is a pseudonymous id scoped to the trip owner.
    """
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner)
        project_id = row.id
        owner_id = row.user_info_id

        visits = sess.exec(
            select(DBShareVisit).where(DBShareVisit.project_id == project_id)
        ).all()

    result: Dict[str, Any] = {
        "full": {"anonymous_count": 0, "registered": []},
        "no_memories": {"anonymous_count": 0, "registered": []},
    }

    registered_ids: Dict[str, List[int]] = {"full": [], "no_memories": []}
    last_seen: Dict[str, Dict[int, float]] = {"full": {}, "no_memories": {}}

    for v in visits:
        bucket = v.token_type if v.token_type in result else "full"
        if v.visitor_type == "anonymous":
            result[bucket]["anonymous_count"] += 1
        else:
            if v.user_info_id is not None:
                registered_ids[bucket].append(v.user_info_id)
                last_seen[bucket][v.user_info_id] = v.last_seen_at

    with get_session() as sess:
        for bucket in ("full", "no_memories"):
            ids = registered_ids[bucket]
            if not ids:
                continue
            users = sess.exec(
                select(UserInfo).where(UserInfo.id.in_(ids))
            ).all()
            # Sign-in names, batched, so a name that is the visitor's address
            # shows as "Traveller" (#507).
            local_ids = {u.local_auth_id for u in users
                         if u.local_auth_id is not None}
            usernames = {
                lu.id: lu.username for lu in sess.exec(
                    select(LocalUser).where(LocalUser.id.in_(local_ids))
                ).all()
            } if local_ids else {}
            result[bucket]["registered"] = [
                {
                    "visitor_key": _visitor_key(owner_id, u.id),
                    "display_name": public_name_given_username(
                        u, usernames.get(u.local_auth_id)),
                    "avatar_url": u.avatar_url,
                    "last_seen_at": last_seen[bucket].get(u.id, 0.0),
                }
                for u in users
            ]

    return result
