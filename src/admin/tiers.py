"""Per-user encryption tier for the admin dashboard.

Read from ``UserInfo.encryption_enabled`` and the user's ``DBRecoveryWrap``
method, then fed through the pure :func:`tier_from` mapping. The tier is
informational: the login password is not part of the E2EE key chain, so an
admin password reset never touches encrypted data whatever the tier (#630).

Tiers, ordered by strength:
  * none   — no encryption
  * low    — server-held escrow (recoverable by the operator)
  * medium — Q&A / security-question recovery
  * high   — recovery key / passphrase (zero-knowledge; unrecoverable server-side)
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlmodel import select

from models.project_db import DBRecoveryWrap
from models.user import UserInfo

_METHOD_TO_TIER = {
    "escrow": "low",
    "qna": "medium",
    "recovery_key": "high",
    "passphrase": "high",
}

_TIER_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def tier_from(enabled: bool, method: str | None) -> str:
    """Pure mapping from (enabled, method) to an encryption tier string.

    Disabled encryption is always ``"none"`` regardless of method. An unknown or
    missing method on an enabled account falls back to ``"none"`` (fail safe: an
    unrecognised scheme is not assumed recoverable).
    """
    if not enabled:
        return "none"
    return _METHOD_TO_TIER.get(method or "", "none")


def user_encryption_tiers(sess, user_info_ids: Iterable[int]) -> dict[int, str]:
    """Encryption tier for each of ``user_info_ids``, in at most two queries.

    A user holding several recovery wraps reports the strongest one. An id with
    no ``UserInfo`` row reports ``"none"``.
    """
    tiers = {uid: "none" for uid in user_info_ids}
    if not tiers:
        return tiers
    enabled = set(sess.exec(
        select(UserInfo.id).where(
            UserInfo.id.in_(tiers), UserInfo.encryption_enabled == True  # noqa: E712
        )
    ).all())
    if not enabled:
        return tiers
    for uid, method in sess.exec(
        select(DBRecoveryWrap.user_info_id, DBRecoveryWrap.method)
        .where(DBRecoveryWrap.user_info_id.in_(enabled))
    ).all():
        tier = tier_from(True, method)
        if _TIER_RANK[tier] > _TIER_RANK[tiers[uid]]:
            tiers[uid] = tier
    return tiers


def user_encryption_tier(sess, user_info_id: int) -> str:
    """Encryption tier for one user. See :func:`user_encryption_tiers`."""
    return user_encryption_tiers(sess, [user_info_id])[user_info_id]
