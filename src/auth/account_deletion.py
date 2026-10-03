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
import time

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
    DBPosterJob,
    DBRouteJob,
    DBVideoJob,
    DBActivity,
    DBActivityGeoPrepared,
    DBShareMemoryContent,
    DBShareVisit,
    DBStravaCache,
    DBDeviceKey,
    DBJournalEntry,
    DBRecoveryWrap,
)
from models.billing import Subscription, SubscriptionRefund, UserUsage
from models.user import (
    EmailVerification,
    ImmichToken,
    LocalUser,
    PolarstepsToken,
    StravaToken,
    UserInfo,
)
from src.auth.strava_deauth import deauthorize_strava
from src.project.repo_core import bump_lock_version
from src.admin import storage as _storage_mod
from src.billing.gateway import GatewayError, get_gateway
from src.billing.refunds import withdrawal_window_open
from src.billing.subscriptions import lock_account
from src.billing.withdrawal import (
    DONE,
    FINAL,
    SETTLED,
    AccountGone,
    NotEligible,
    RefundInProgress,
    finish_pending,
    ledger_state,
    request_withdrawal,
    settle,
)
from src.exceptions.errors import AccountDeletionRefused
from src.utils.logging import get_logger

_log = get_logger(__name__)

#: Refusal code for "may still bill, and no gateway to cancel it with".
BILLING_UNAVAILABLE = "billing_unavailable"

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
        _log.warning(
            "Deletion of account %s refused: status %r may still bill customer "
            "%r and billing is not configured. Resolve it as described under "
            "\"Deleting an account\" in docs/BILLING.md.",
            user_info_id, row.status, customer_id,
        )
        raise AccountDeletionRefused(
            "This account has a paid plan that this server cannot cancel, "
            "because billing is not configured here. The account was not "
            "deleted. Please contact the administrator.",
            status_code=409, code=BILLING_UNAVAILABLE,
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


def _window_state(sess: Session, user_info_id: int) -> tuple | None:
    """``(customer, contract start, contract subscription)``.

    Read as columns, fresh from the database, not off an ORM row the session
    may have loaded earlier with attributes a webhook has since changed.
    """
    row = sess.execute(
        select(
            Subscription.provider_customer_id,
            Subscription.contract_started_at,
            Subscription.contract_subscription_id,
        ).where(Subscription.user_info_id == user_info_id)
    ).first()
    return tuple(row) if row is not None else None


def record_deletion_withdrawal(sess: Session, user_info_id: int, now: float) -> None:
    """A deletion inside the window is a withdrawal: record it first (#441).

    Before anything is cancelled, like ``POST /withdraw``: the current
    contract's ledger row, with the request time and the contract's invoice
    frozen. A cancellation that then fails leaves it pending, and the
    deletion retried — even after the deadline — completes the same
    withdrawal. A contract whose start is not on record yet is handled after
    the cancellation, from Stripe (:func:`refund_inside_window`).
    """
    state = _window_state(sess, user_info_id)
    if state is None:
        return
    customer_id, start, subscription_id = state
    if not customer_id or not subscription_id or not start:
        return
    ledger = ledger_state(sess, subscription_id)
    if ledger in FINAL or (not ledger and not withdrawal_window_open(start, now)):
        return
    gateway = get_gateway()
    if gateway is None:
        return
    try:
        request_withdrawal(gateway, user_info_id=user_info_id, customer_id=customer_id,
                           subscription_id=subscription_id, contract_start=start,
                           requested_at=now)
    except AccountGone:
        return
    except RefundInProgress as exc:
        raise AccountDeletionRefused(
            "A refund for your plan is being processed right now, so the "
            "account was not deleted yet. Please try again in a few minutes.",
            status_code=409, code="refund_in_progress",
        ) from exc
    except GatewayError as exc:
        _log.error("Deletion of account %s refused: the withdrawal could not be "
                   "recorded at Stripe: %s", user_info_id, exc)
        raise AccountDeletionRefused(
            "The billing service could not be reached, so nothing was cancelled "
            "and the account was not deleted. Please try again in a few minutes.",
            status_code=502, code="refund_failed",
        ) from exc


def refund_inside_window(sess: Session, user_info_id: int, now: float) -> None:
    """Refund pro rata what deletion cancelled, inside the withdrawal window (#441).

    Runs after :func:`cancel_live_subscription` — so every cancellation has
    landed — and before any row is deleted:

    * every **pending** refund of the customer is completed, of any contract:
      its cancellation landed in time, so it is owed whatever came after;
    * the current contract's subscription is refunded if its window is open
      and its refund is not in the ledger yet. A ``done``, ``owed`` or
      ``settled`` refund is final and is not sent to Stripe again.

    Everything goes through the ledger (``src.billing.withdrawal.settle``), so
    a refund is made once, whatever else refunded it or is refunding it now.

    When the row has no contract start yet — the payment's webhook has not
    arrived — the start is taken from Stripe: the customer's most recently
    started paid-for subscription.

    Refuses the deletion (:class:`AccountDeletionRefused`) with 502 on a
    transient failure and 409 while another request is refunding; nothing is
    deleted and the user can retry. A refund recorded as *owed* does not
    refuse it: that record outlives the account, for the owner to settle
    (owner decision 2026-09-26).
    """
    state = _window_state(sess, user_info_id)
    if state is None:
        return
    customer_id, start, subscription_id = state
    # Every webhook that records a purchase names the customer, so a contract
    # always has one.
    if not customer_id:
        return
    gateway = get_gateway()
    if gateway is None:
        if start and subscription_id and withdrawal_window_open(start, now):
            # Only reachable when nothing may still bill (cancel_live_
            # subscription refused otherwise) and billing was switched off.
            _log.warning(
                "Deletion of account %s inside the withdrawal window: billing "
                "is not configured, so customer %s must be refunded by hand",
                user_info_id, customer_id,
            )
        return
    refunds = []
    try:
        if not start:
            latest = gateway.latest_subscription(customer_id)
            if latest is not None:
                subscription_id, start = latest
        refunds += finish_pending(gateway, user_info_id=user_info_id,
                                  customer_id=customer_id, now=now)
        if (subscription_id and ledger_state(sess, subscription_id) == ""
                and withdrawal_window_open(start, now)):
            try:
                refunds.append(settle(
                    gateway, user_info_id=user_info_id, customer_id=customer_id,
                    subscription_id=subscription_id, contract_start=start,
                    requested_at=now, now=now))
            except NotEligible:
                pass
    except RefundInProgress as exc:
        raise AccountDeletionRefused(
            "A refund for your plan is being processed right now, so the "
            "account was not deleted yet. Please try again in a few minutes.",
            status_code=409, code="refund_in_progress",
        ) from exc
    except AccountGone:
        return  # a concurrent deletion of this account got there first
    except GatewayError as exc:
        # ERROR, not a warning: a refusal Stripe keeps giving blocks the
        # deletion until someone looks (docs/BILLING.md, "Owed refunds").
        _log.error("Deletion of account %s refused: the refund failed and is "
                   "retried on the next attempt: %s", user_info_id, exc)
        raise AccountDeletionRefused(
            "Your paid plan was cancelled, but the refund for the unused part "
            "could not be issued, so the account was not deleted. Please try "
            "again later: it will still be refunded after the 14 days, and "
            "never twice.",
            status_code=502, code="refund_failed",
        ) from exc
    for refund in refunds:
        if refund.amount_cents or refund.owed_cents:
            _log.info("Deletion of account %s: refunded %s %s, owed %s",
                      user_info_id, refund.amount_cents, refund.currency,
                      refund.owed_cents)


def _billing_state(sess: Session, user_info_id: int) -> tuple | None:
    """What deletion settles billing from: customer, subscription, status.

    Read as columns, not as the ORM row: the session's identity map would hand
    back the object loaded earlier, with its stale attributes.
    """
    row = sess.execute(
        select(
            Subscription.provider_customer_id,
            Subscription.provider_subscription_id,
            Subscription.status,
        ).where(Subscription.user_info_id == user_info_id)
    ).first()
    return tuple(row) if row is not None else None


def _billing_moved(settled: tuple | None, current: tuple | None) -> bool:
    """True when billing changed in a way that might leave something billing.

    Deletion's own cancellation makes Stripe send
    ``customer.subscription.deleted``, which can land before the deletion takes
    the lock and rewrite the row to that (other, or same) subscription as
    ``canceled``. That is the cancellation succeeding, not a reason to refuse.
    What must stop the deletion is a customer it never settled, or a
    subscription left in a state that can still bill. A row that vanished is
    treated as moved: only a deletion removes it, and a deleted account is
    caught before this is asked.
    """
    if current == settled:
        return False
    if current is None:
        return True
    customer, _subscription, status = current
    if customer != (settled[0] if settled else ""):
        return True
    return (status or "") not in _ENDED_SUBSCRIPTION_STATUSES


def delete_user_and_data(sess: Session, user_info_id: int) -> None:
    """Delete a ``UserInfo`` and every row it owns, directly or via a project.

    Cancels any subscription that may still bill first — and, inside the
    withdrawal window, refunds its unused part — and raises
    :class:`AccountDeletionRefused`, with nothing deleted, if either fails.

    Commits internally. Does not touch the filesystem — call
    :func:`purge_user_files` afterwards, outside any DB session.
    """
    settled = _billing_state(sess, user_info_id)
    now = time.time()
    record_deletion_withdrawal(sess, user_info_id, now)
    cancel_live_subscription(sess, user_info_id)
    refund_inside_window(sess, user_info_id, now)
    # Revoke the app at Strava (issue #440) once billing is settled, so a
    # refused deletion leaves Strava connected; before the account lock, like
    # the Stripe calls above, since it is a network round trip.
    # Only the token fields are kept, so no ORM row is held across the call.
    # Best effort — the token row goes below whatever Strava answered.
    strava_token = sess.exec(
        select(StravaToken).where(StravaToken.user_info_id == user_info_id)
    ).first()
    revoked_refresh = None
    if strava_token is not None:
        access, revoked_refresh = strava_token.access_token, strava_token.refresh_token
        deauthorize_strava(user_info_id, access, revoked_refresh)
    # Stripe was called without holding any lock (never hold SQLite's write
    # lock across the network). A webhook can land in that window and record
    # a first purchase on the row — a customer this deletion never cancelled.
    # So take the lock the webhook takes too, and only go on if billing is
    # still what was settled; otherwise roll back and let the retry cancel it.
    lock_account(sess, user_info_id)
    if sess.execute(
        select(UserInfo.id).where(UserInfo.id == user_info_id)
    ).first() is None:
        # A concurrent deletion of the same account won the lock and has
        # committed. What this call was asked to do is done; reporting "billing
        # changed" (or an error at all) would tell the user their account is
        # still there.
        sess.rollback()
        return
    if _billing_moved(settled, _billing_state(sess, user_info_id)):
        sess.rollback()
        raise AccountDeletionRefused(
            "Your billing changed while the account was being deleted, so "
            "nothing was removed. Please try again.",
            status_code=409, code="billing_changed",
        )
    # A Strava refresh in flight may have rotated the tokens while Strava was
    # being called above (review R3-1): its callback commits the new ones,
    # and the row would go below still valid. Re-read under the lock — every
    # later write queues behind it — and, if the refresh token is not the one
    # revoked, revoke the row's tokens after the final commit, never here.
    # Columns, not the entity: the row object loaded above is in the identity
    # map and a re-select would hand back its stale attributes.
    rotated = None
    current_token = sess.execute(
        select(StravaToken.access_token, StravaToken.refresh_token)
        .where(StravaToken.user_info_id == user_info_id)
    ).first()
    if current_token is not None and current_token.refresh_token != revoked_refresh:
        rotated = (current_token.access_token, current_token.refresh_token)

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

    # Jobs this user started, on their own trips or as a companion on someone
    # else's, removed before the projects they point at. Their output files
    # live under ``data/users/{id}/`` (posters/, videos/) and go with
    # purge_user_files. Jobs another user started on this user's trips are
    # that user's rows, with files under that user's folder, and stay.
    _delete_all(DBPosterJob, DBPosterJob.user_info_id == user_info_id)
    _delete_all(DBVideoJob, DBVideoJob.user_info_id == user_info_id)
    _delete_all(DBRouteJob, DBRouteJob.user_info_id == user_info_id)

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
    _delete_all(ImmichToken, ImmichToken.user_info_id == user_info_id)
    # Billing rows (issue #121). Any subscription that could still bill was
    # cancelled at the provider by cancel_live_subscription above (issue #429).
    # The refund ledger (#441) is keyed by Stripe customer, not by account:
    # finished refunds (done, settled) go with the account; one recorded as
    # owed stays until the owner settles it, as the privacy policy says.
    customer_ids = [c for c in sess.exec(
        select(Subscription.provider_customer_id)
        .where(Subscription.user_info_id == user_info_id)
    ).all() if c]
    if customer_ids:
        _delete_all(SubscriptionRefund,
                    SubscriptionRefund.customer_id.in_(customer_ids),
                    SubscriptionRefund.state.in_((DONE, SETTLED)))
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
    if rotated is not None:
        deauthorize_strava(user_info_id, *rotated)


def purge_user_files(user_id: int) -> None:
    """Remove ``data/users/{id}/`` (photos, avatars, …). No-op if absent."""
    shutil.rmtree(_storage_mod._user_dir(user_id), ignore_errors=True)
