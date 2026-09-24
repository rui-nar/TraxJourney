"""Full account deletion — every row and file owned by a user.

Shared by the self-service ``DELETE /api/auth/me`` and the admin-triggered
``DELETE /api/admin/users/{id}``. A single implementation avoids the two paths
drifting out of sync (or one of them leaking data the other cleans up).

DB rows are deleted first (children before parents, matching FK direction even
though SQLite enforcement is off in this project — Postgres deployments do
enforce it). On-disk files live under ``data/users/{id}/`` and must be purged
*outside* the DB session, mirroring the storage-walk pattern in
``src.admin.storage``.
"""
from __future__ import annotations

import shutil

from sqlmodel import Session, select

from models.project_db import (
    DBEncounter,
    DBMemory,
    DBMemoryComment,
    DBMemoryLike,
    DBMemoryTranslation,
    DBPerson,
    DBPersonGroup,
    DBProject,
    DBProjectInvite,
    DBProjectItem,
    DBProjectMember,
    DBProjectPendingInvite,
    DBProjectSyncMeta,
    DBActivity,
    DBActivityGeoPrepared,
    DBShareMemoryContent,
    DBShareVisit,
    DBStravaCache,
    DBDeviceKey,
    DBJournalEntry,
    DBRecoveryWrap,
)
from models.billing import Subscription, UserUsage
from models.user import (
    EmailVerification,
    LocalUser,
    PolarstepsToken,
    StravaToken,
    UserInfo,
)
from src.project.repo_core import bump_lock_version
from src.admin import storage as _storage_mod
from src.billing.gateway import GatewayError, get_gateway
from src.exceptions.errors import AccountDeletionRefused

#: Provider statuses after which a subscription can never charge again.
#: Deliberately the complement of "may still bill" rather than a list of live
#: statuses: ``entitlements._LIVE_STATUSES`` answers "does this grant the plan",
#: which leaves out ``unpaid`` (invoices keep being generated), ``incomplete``
#: (the first invoice can still be paid) and ``paused`` (can resume) — all of
#: which could still take money from someone with no account left. An unknown
#: future status is cancelled too; cancelling one that turns out to be over
#: costs a round trip, not a refusal. ``none`` is a row that never had a
#: subscription (a free user, or an admin comp).
_ENDED_SUBSCRIPTION_STATUSES = frozenset({"", "none", "canceled", "incomplete_expired"})


def cancel_live_subscription(sess: Session, user_info_id: int) -> list[str]:
    """Stop everything that could still bill the user, before deleting (#429).

    Must run before any row is deleted: if it raises, the account is left
    exactly as it was, so the user can retry — and a retry is safe because the
    gateway treats an already-ended subscription as success. Writes nothing.
    Returns the ids of the subscriptions it cancelled.

    Once the account has a provider customer, the provider decides, not our
    cached row: the row can say "none" while a checkout page opened earlier is
    still waiting to be paid, and it tracks only one subscription. So every
    open checkout of that customer is expired and every running subscription
    cancelled. Only a row with no customer falls back to its one stored
    subscription.

    Raises :class:`AccountDeletionRefused` when something may still bill and
    could not be stopped — including when this deployment has no payment
    gateway configured, since deleting the account would then orphan a paying
    customer. A user who never reached the provider never touches the gateway,
    so deletion works on an instance without billing.
    """
    row = sess.exec(
        select(Subscription).where(Subscription.user_info_id == user_info_id)
    ).first()
    if row is None:
        return []
    customer_id = row.provider_customer_id or ""
    subscription_id = row.provider_subscription_id or ""
    may_bill = (row.status or "") not in _ENDED_SUBSCRIPTION_STATUSES
    if not customer_id and not (subscription_id and may_bill):
        return []

    gateway = get_gateway()
    if gateway is None:
        if not may_bill:
            # Billing was switched off after this account reached the provider.
            # Nothing can be asked, and our last word is that nothing runs;
            # refusing would make the account undeletable for good.
            return []
        # 409, not 502: nothing upstream failed and retrying will not help —
        # the server is not in a state where this deletion can be done.
        raise AccountDeletionRefused(
            "This account has a paid plan that this server cannot cancel, "
            "because billing is not configured here. The account was not "
            "deleted. Please contact the administrator.",
            status_code=409, code="billing_unavailable",
        )
    try:
        if customer_id:
            return gateway.cancel_all_for_customer(customer_id)
        gateway.cancel_subscription(subscription_id)
        return [subscription_id]
    except GatewayError as exc:
        # 502: the payment provider failed. Retrying later may succeed.
        raise AccountDeletionRefused(
            "The paid plan on this account could not be cancelled, so the "
            "account was not deleted and nothing was removed. Please try "
            "again in a few minutes.",
            status_code=502, code="subscription_cancel_failed",
        ) from exc


def delete_user_and_data(sess: Session, user_info_id: int) -> None:
    """Delete a ``UserInfo`` and every row it owns, directly or via a project.

    Cancels any subscription that may still bill first, and raises
    :class:`AccountDeletionRefused` — with nothing deleted — if that fails.

    Commits internally. Does not touch the filesystem — call
    :func:`purge_user_files` afterwards, outside any DB session.
    """
    cancel_live_subscription(sess, user_info_id)

    project_ids = sess.exec(
        select(DBProject.id).where(DBProject.user_info_id == user_info_id)
    ).all()
    memory_ids = sess.exec(
        select(DBMemory.id).where(DBMemory.project_id.in_(project_ids))
    ).all() if project_ids else []

    def _delete_all(model, *conditions) -> None:
        for row in sess.exec(select(model).where(*conditions)).all():
            sess.delete(row)

    if memory_ids:
        _delete_all(DBMemoryComment, DBMemoryComment.memory_id.in_(memory_ids))
        _delete_all(DBMemoryLike, DBMemoryLike.memory_id.in_(memory_ids))
        _delete_all(DBMemoryTranslation, DBMemoryTranslation.memory_id.in_(memory_ids))
        _delete_all(DBShareMemoryContent, DBShareMemoryContent.memory_id.in_(memory_ids))
    # Comments/likes/visits this user made on other people's shared projects.
    _delete_all(DBMemoryComment, DBMemoryComment.user_info_id == user_info_id)
    _delete_all(DBMemoryLike, DBMemoryLike.user_info_id == user_info_id)
    _delete_all(DBShareVisit, DBShareVisit.user_info_id == user_info_id)

    # Travel-companion footprint (issue #106): rows this user left in OTHER
    # users' projects, and membership/invite rows in both directions. Items
    # first (they reference the journal entries / activities being removed);
    # own-project rows are covered again by the project block below, which is
    # harmless. The user's journal photo files live under
    # ``data/users/{id}/journal/`` and are removed by purge_user_files.
    authored_journal_ids = sess.exec(
        select(DBJournalEntry.id).where(DBJournalEntry.user_info_id == user_info_id)
    ).all()
    activity_ids = sess.exec(
        select(DBActivity.id).where(DBActivity.user_info_id == user_info_id)
    ).all()
    # The item rows about to go can belong to *other* users' shared projects
    # (issue #106). Collect those projects before deleting, so the lock can be
    # advanced for each: a structural save_project that loaded before this
    # deletion would otherwise pass its compare-and-swap and re-insert the item
    # rows from its snapshot, pointing at journal entries and activities that no
    # longer exist. Same hazard the delete_* endpoints carry (issue #173).
    touched_project_ids: set[int] = set()
    for column, ids_ in ((DBProjectItem.journal_id, authored_journal_ids),
                         (DBProjectItem.activity_id, activity_ids)):
        if ids_:
            touched_project_ids.update(sess.exec(
                select(DBProjectItem.project_id).where(column.in_(ids_))
            ).all())
    # Bump before the deletes, not after: bump_lock_version issues a Core
    # UPDATE, which autoflushes whatever is pending, so bumping last would take
    # the item rows before the project row — the inverse of save_project's
    # order, and the lock-order inversion issue #398 documents. Harmless on
    # SQLite (single writer) but free to get right here.
    for touched in touched_project_ids:
        bump_lock_version(sess, touched)
    if authored_journal_ids:
        _delete_all(DBProjectItem, DBProjectItem.journal_id.in_(authored_journal_ids))
        _delete_all(DBJournalEntry, DBJournalEntry.id.in_(authored_journal_ids))
    if activity_ids:
        _delete_all(DBProjectItem, DBProjectItem.activity_id.in_(activity_ids))
        _delete_all(DBActivityGeoPrepared,
                    DBActivityGeoPrepared.activity_id.in_(activity_ids))
    _delete_all(DBProjectMember, DBProjectMember.user_info_id == user_info_id)
    _delete_all(DBProjectInvite, DBProjectInvite.created_by == user_info_id)
    # Pending invites (issue #110) point at the sender via invited_by, so they
    # outlive them as dangling rows unless removed here.
    _delete_all(DBProjectPendingInvite,
                DBProjectPendingInvite.invited_by == user_info_id)
    _delete_all(EmailVerification,
                EmailVerification.user_info_id == user_info_id)
    if project_ids:
        _delete_all(DBProjectMember, DBProjectMember.project_id.in_(project_ids))
        _delete_all(DBProjectInvite, DBProjectInvite.project_id.in_(project_ids))
        _delete_all(DBProjectPendingInvite,
                    DBProjectPendingInvite.project_id.in_(project_ids))

    if project_ids:
        _delete_all(DBMemory, DBMemory.project_id.in_(project_ids))
        _delete_all(DBJournalEntry, DBJournalEntry.project_id.in_(project_ids))
        _delete_all(DBEncounter, DBEncounter.project_id.in_(project_ids))
        _delete_all(DBPerson, DBPerson.project_id.in_(project_ids))
        _delete_all(DBPersonGroup, DBPersonGroup.project_id.in_(project_ids))
        _delete_all(DBShareVisit, DBShareVisit.project_id.in_(project_ids))
        _delete_all(DBProjectSyncMeta, DBProjectSyncMeta.project_id.in_(project_ids))
        _delete_all(DBProjectItem, DBProjectItem.project_id.in_(project_ids))
        _delete_all(DBProject, DBProject.id.in_(project_ids))

    _delete_all(DBActivity, DBActivity.user_info_id == user_info_id)
    _delete_all(DBStravaCache, DBStravaCache.user_info_id == user_info_id)
    _delete_all(StravaToken, StravaToken.user_info_id == user_info_id)
    _delete_all(PolarstepsToken, PolarstepsToken.user_info_id == user_info_id)
    _delete_all(DBDeviceKey, DBDeviceKey.user_info_id == user_info_id)
    _delete_all(DBRecoveryWrap, DBRecoveryWrap.user_info_id == user_info_id)
    # Billing rows (issue #121). Any subscription that could still bill was
    # cancelled at the provider by cancel_live_subscription above (issue #429).
    _delete_all(Subscription, Subscription.user_info_id == user_info_id)
    _delete_all(UserUsage, UserUsage.user_info_id == user_info_id)

    user_info = sess.get(UserInfo, user_info_id)
    local_auth_id = user_info.local_auth_id if user_info else None
    if user_info is not None:
        sess.delete(user_info)
    sess.flush()

    if local_auth_id is not None:
        local_user = sess.get(LocalUser, local_auth_id)
        if local_user is not None:
            sess.delete(local_user)

    sess.commit()


def purge_user_files(user_id: int) -> None:
    """Remove ``data/users/{id}/`` (photos, avatars, …). No-op if absent."""
    shutil.rmtree(_storage_mod._user_dir(user_id), ignore_errors=True)
