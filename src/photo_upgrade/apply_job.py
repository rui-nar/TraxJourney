"""The apply job chain: the selected suggestions of a scan become originals
in the photo store (issue #33, U11 of docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md;
D3, D4, D5, D9, D12, OD3).

An apply is a chain of short jobs on the ``photos`` queue (D9). Each
``run_apply_batch`` takes up to ``APPLY_BATCH`` selected ``proposed`` rows,
in trip order, and for each one:

1. moves it ``proposed → applying`` and beats ``apply_heartbeat_at``, in one
   transaction that only commits while the apply is ``running``;
2. re-checks the runner's right to it (D3) and that the photo is still
   listed, else the row goes ``stale``;
3. downloads the photo from Immich (OD3): a JPEG through Immich's full-size
   rendition, a HEIC, HEIF, PNG or WebP as its original. Anything else, RAW
   included, fails with ``unsupported_format``; an original over 25 MB
   (D12) with ``too_large``;
4. replaces the photo through ``api/photo_replace.replace_photo_bytes`` with
   ``source="immich:<asset id>"``, which converts a HEIC to JPEG (D4) and
   deletes the old files once the list is written (D5).

The apply's ``apply_status`` goes ``pending → running`` as each batch starts,
and back to ``pending`` as it hands over to the next one, so the startup sweep
of a restarted worker leaves an apply waiting between batches alone. When no
selected ``proposed`` row is left, it is ``done``. A full account stops it as
``stopped``/``storage_full``, a killed or failed batch as
``stopped``/``interrupted``; :func:`start_apply` resumes either.

A row left ``applying`` by a killed batch is recovered by
:func:`recover_interrupted_apply`: the replace writes the photo list in one
compare-and-set, so the old photo still listed means it never happened.

Every state change is a compare-and-set, as in ``src/video/job_runner.py``.
Failures are logged by exception type, never by message: an Immich error can
quote the server's address. The key is never logged.
"""
from __future__ import annotations

import json
import time
import traceback
from typing import Iterable, List, Optional

import urllib3
from sqlalchemy import func, update
from sqlmodel import select

from api.photo_order import load_state
from api.photo_replace import PhotoGone, replace_photo_bytes
from api.photo_write import PhotoWriteConflict
from models.db import get_session
from models.project_db import (
    DBJournalEntry,
    DBMemory,
    DBPhotoUpgradeScan,
    DBPhotoUpgradeSuggestion,
    DBProject,
)
from models.user import ImmichToken
from src.exceptions.errors import QuotaExceeded
from src.immich.client import ImmichClient, ImmichError, ImmichRejected
from src.jobs.queue import QUEUE_PHOTOS, enqueue, queue_available
from src.utils.logging import get_logger
from src.utils.photo_store import MAX_PHOTO_BYTES, InvalidPhoto, PhotoTooLarge
from src.utils.safe_fetch import FetchRefused

_log = get_logger(__name__)

#: Rows applied per job (D9).
APPLY_BATCH = 25
#: No row is started once a batch has run this long, so a slow Immich server
#: hands over to the next job well inside RQ's 600 s timeout.
BATCH_BUDGET_S = 300
#: The batch's clock; tests replace it.
_clock = time.monotonic

IN_PROGRESS = ("pending", "running")
#: What :func:`start_apply` starts or resumes from. ``pending`` too, for a
#: caller that set it in its own transaction; a second job then finds the
#: apply ``running`` or finished and stops quietly.
_STARTABLE = ("idle", "pending", "stopped", "done")

# ``apply_stopped_reason``.
STOPPED_STORAGE_FULL = "storage_full"
STOPPED_INTERRUPTED = "interrupted"

# ``error_code`` on a row that did not apply; the app maps each to a sentence.
TOO_LARGE = "too_large"
UNSUPPORTED_FORMAT = "unsupported_format"
NOT_FOUND_IN_IMMICH = "not_found_in_immich"
IMMICH_UNREACHABLE = "immich_unreachable"
PHOTO_GONE = "photo_gone"
STORAGE_FULL = "storage_full"
INVALID_IMAGE = "invalid_image"

# OD3: a JPEG is taken from Immich's full-size rendition, a HEIC or HEIF from
# its original (stored as JPEG, D4). PNG and WebP originals are stored as
# they come, as an upload of them is. RAW and every other type are refused.
_FULLSIZE_TYPES = frozenset({"image/jpeg", "image/jpg", "image/pjpeg"})
_ORIGINAL_TYPES = frozenset({"image/heic", "image/heif", "image/png", "image/webp"})
#: Immich answers 400 "Not found or no access" for an asset it does not have.
_NOT_FOUND_STATUSES = frozenset({400, 403, 404, 410})

_MEMBER_ROLES = ("owner", "co-owner", "editor")


class _RowEnds(Exception):
    """Ends one row in *state*, with *code* as its ``error_code``."""

    def __init__(self, state: str, code: Optional[str]):
        super().__init__(state)
        self.state = state
        self.code = code


def _describe(exc: BaseException) -> str:
    """*exc*'s type and traceback, without its message: an Immich error's
    message can quote the server's address."""
    return type(exc).__qualname__ + "\n" + "".join(traceback.format_tb(exc.__traceback__))


# ── Row helpers ───────────────────────────────────────────────────────────────

def _cas_apply(scan_id: int, from_states: Iterable[str], *where, **values) -> bool:
    """Move the scan's apply to *values* only if its ``apply_status`` is one
    of *from_states* (and *where* holds). True when this call made the change."""
    with get_session() as sess:
        result = sess.execute(
            update(DBPhotoUpgradeScan)
            .where(DBPhotoUpgradeScan.id == scan_id,
                   DBPhotoUpgradeScan.apply_status.in_(tuple(from_states)), *where)
            .values(**values)
        )
        sess.commit()
        return result.rowcount == 1


def _stop(scan_id: int, from_states: Iterable[str], reason: str) -> bool:
    return _cas_apply(scan_id, from_states, apply_status="stopped",
                      apply_stopped_reason=reason, apply_heartbeat_at=time.time())


def _end_row(row_id: int, state: str, **values) -> None:
    """Move an ``applying`` row to *state*. A row recovered meanwhile is left
    as recovery set it."""
    with get_session() as sess:
        sess.execute(
            update(DBPhotoUpgradeSuggestion)
            .where(DBPhotoUpgradeSuggestion.id == row_id,
                   DBPhotoUpgradeSuggestion.state == "applying")
            .values(state=state, **values)
        )
        sess.commit()


def _take(scan_id: int, row_id: int) -> Optional[bool]:
    """Beat the apply's heartbeat and move the row ``proposed → applying``,
    in one transaction. The heartbeat UPDATE comes first and takes SQLite's
    write lock, so no row is taken for an apply stopped meanwhile. None when
    the apply is no longer ``running``; else whether the row was taken."""
    with get_session() as sess:
        beat = sess.execute(
            update(DBPhotoUpgradeScan)
            .where(DBPhotoUpgradeScan.id == scan_id,
                   DBPhotoUpgradeScan.apply_status == "running")
            .values(apply_heartbeat_at=time.time())
        )
        if beat.rowcount != 1:
            sess.rollback()
            return None
        took = sess.execute(
            update(DBPhotoUpgradeSuggestion)
            .where(DBPhotoUpgradeSuggestion.id == row_id,
                   DBPhotoUpgradeSuggestion.state == "proposed",
                   DBPhotoUpgradeSuggestion.selected == True)  # noqa: E712
            .values(state="applying", error_code=None)
        )
        sess.commit()
        return took.rowcount == 1


def _to_apply(scan_id: int):
    """Selected ``proposed`` rows of the scan with an asset, in trip order."""
    return (select(DBPhotoUpgradeSuggestion.id)
            .where(DBPhotoUpgradeSuggestion.scan_id == scan_id,
                   DBPhotoUpgradeSuggestion.state == "proposed",
                   DBPhotoUpgradeSuggestion.selected == True,  # noqa: E712
                   DBPhotoUpgradeSuggestion.asset_id.is_not(None)))


def _make_client(server_url: str, api_key: str) -> ImmichClient:
    """The runner's Immich client. Tests replace this function."""
    return ImmichClient(server_url, api_key)


def _client_for(user_info_id: int) -> Optional[ImmichClient]:
    """The runner's client, or None when Immich is no longer connected."""
    with get_session() as sess:
        tok = sess.exec(
            select(ImmichToken).where(ImmichToken.user_info_id == user_info_id)
        ).first()
        if tok is None or not tok.api_key or not tok.server_url:
            return None
        server_url, api_key = tok.server_url, tok.api_key
    return _make_client(server_url, api_key)


# ── One row ───────────────────────────────────────────────────────────────────

def _owner_dir(scan: DBPhotoUpgradeScan, row: DBPhotoUpgradeSuggestion) -> str:
    """The user whose tree holds the row's photo, once the runner's right to
    it is re-checked (D3) and the photo is found still listed. Raises
    ``_RowEnds`` with ``stale`` otherwise: a rescan then says why."""
    from api.project_access import effective_role

    runner = scan.user_info_id
    with get_session() as sess:
        project = sess.get(DBProject, scan.project_id)
        if project is None:
            raise _RowEnds("stale", PHOTO_GONE)
        role = effective_role(sess, project, runner)
        if row.artifact_kind == "memory":
            if role != "owner":
                raise _RowEnds("stale", None)
            artifact = sess.exec(select(DBMemory.project_id, DBMemory.photos_json)
                                 .where(DBMemory.id == row.artifact_id)).first()
            owner_dir = project.user_info_id
        else:
            if role not in _MEMBER_ROLES:
                raise _RowEnds("stale", None)
            artifact = sess.exec(select(DBJournalEntry.project_id, DBJournalEntry.photos_json,
                                        DBJournalEntry.user_info_id)
                                 .where(DBJournalEntry.id == row.artifact_id)).first()
            if artifact is not None:
                author = artifact.user_info_id
                if author != runner and not (author is None and runner == project.user_info_id):
                    raise _RowEnds("stale", None)
            owner_dir = runner
    if artifact is None or artifact.project_id != project.id:
        raise _RowEnds("stale", PHOTO_GONE)
    if row.photo_uuid not in json.loads(artifact.photos_json or "[]"):
        raise _RowEnds("stale", PHOTO_GONE)
    return str(owner_dir)


def _download(client: Optional[ImmichClient], row: DBPhotoUpgradeSuggestion) -> bytes:
    """The bytes to store for the row's asset (OD3, D12), or ``_RowEnds``
    with ``failed`` and the reason."""
    mime = (row.asset_mime or "").lower()
    if mime not in _FULLSIZE_TYPES and mime not in _ORIGINAL_TYPES:
        raise _RowEnds("failed", UNSUPPORTED_FORMAT)
    if row.asset_bytes is not None and row.asset_bytes > MAX_PHOTO_BYTES:
        raise _RowEnds("failed", TOO_LARGE)
    if client is None:
        raise _RowEnds("failed", IMMICH_UNREACHABLE)
    try:
        if mime in _FULLSIZE_TYPES:
            resp = client.open_fullsize(row.asset_id)
        else:
            resp = client.open_original(row.asset_id)
    except ImmichRejected as exc:
        if exc.status in _NOT_FOUND_STATUSES:
            raise _RowEnds("failed", NOT_FOUND_IN_IMMICH) from None
        raise _RowEnds("failed", IMMICH_UNREACHABLE) from None
    except ImmichError:
        raise _RowEnds("failed", IMMICH_UNREACHABLE) from None
    body = bytearray()
    try:
        for chunk in resp.iter_content():
            body += chunk
            # The size Immich reported is untrusted (E2): the cap is enforced
            # on what arrives, without reading the rest.
            if len(body) > MAX_PHOTO_BYTES:
                raise _RowEnds("failed", TOO_LARGE)
    except (FetchRefused, urllib3.exceptions.HTTPError, OSError, ImmichError):
        raise _RowEnds("failed", IMMICH_UNREACHABLE) from None
    finally:
        resp.close()
    return bytes(body)


def _apply_row(scan: DBPhotoUpgradeScan, row_id: int,
               client: Optional[ImmichClient]) -> bool:
    """Apply one row. False when the chain must stop: the apply is no longer
    ``running``, or the account is full."""
    took = _take(scan.id, row_id)
    if took is None:
        return False
    if not took:
        return True  # unticked or changed since the batch read it
    with get_session() as sess:
        row = sess.get(DBPhotoUpgradeSuggestion, row_id)
        if row is None:
            return True
        sess.expunge(row)

    try:
        owner_dir = _owner_dir(scan, row)
        raw = _download(client, row)
        new_uuid = replace_photo_bytes(
            row.artifact_kind, row.artifact_id, row.photo_uuid, raw,
            owner_dir=owner_dir, source=f"immich:{row.asset_id}")
    except _RowEnds as end:
        _end_row(row_id, end.state, error_code=end.code)
        return True
    except PhotoGone:
        _end_row(row_id, "stale", error_code=PHOTO_GONE)
        return True
    except PhotoWriteConflict:
        # Lost five races with other writers: the next batch tries again.
        _end_row(row_id, "proposed")
        return True
    except QuotaExceeded:
        # The row that met the limit says so; a resume takes it again.
        _end_row(row_id, "proposed", error_code=STORAGE_FULL)
        if _stop(scan.id, ("running",), STOPPED_STORAGE_FULL):
            _log.info("Photo apply %s stopped: storage full", scan.id)
        return False
    except PhotoTooLarge:
        _end_row(row_id, "failed", error_code=TOO_LARGE)
        return True
    except InvalidPhoto:
        _end_row(row_id, "failed", error_code=INVALID_IMAGE)
        return True
    _end_row(row_id, "applied", applied_uuid=new_uuid, error_code=None)
    return True


# ── The chain ─────────────────────────────────────────────────────────────────

def _batch(scan_id: int) -> bool:
    """One batch. True when another must follow; the apply is then
    ``pending`` again."""
    if not _cas_apply(scan_id, ("pending",), apply_status="running",
                      apply_heartbeat_at=time.time()):
        _log.info("Photo apply %s: batch not started, no longer pending", scan_id)
        return False
    # This batch now holds the apply: a row still ``applying`` was left by a
    # batch that died.
    recover_interrupted_apply(scan_id)
    with get_session() as sess:
        scan = sess.get(DBPhotoUpgradeScan, scan_id)
        if scan is None:
            return False
        sess.expunge(scan)
        ids: List[int] = list(sess.exec(
            _to_apply(scan_id)
            .order_by(DBPhotoUpgradeSuggestion.date, DBPhotoUpgradeSuggestion.artifact_kind,
                      DBPhotoUpgradeSuggestion.artifact_id, DBPhotoUpgradeSuggestion.position,
                      DBPhotoUpgradeSuggestion.id)
            .limit(APPLY_BATCH)).all())

    started = _clock()
    client = _client_for(scan.user_info_id) if ids else None
    for done, row_id in enumerate(ids):
        # At least one row a batch, so a chain always moves on.
        if done and _clock() - started > BATCH_BUDGET_S:
            break
        if not _apply_row(scan, row_id, client):
            return False

    with get_session() as sess:
        remaining = sess.exec(select(func.count()).select_from(_to_apply(scan_id).subquery())).one()
    if remaining:
        return _cas_apply(scan_id, ("running",), apply_status="pending",
                          apply_heartbeat_at=time.time())
    if _cas_apply(scan_id, ("running",), apply_status="done", apply_heartbeat_at=time.time()):
        _log.info("Photo apply %s done", scan_id)
    return False


def _run_batch(scan_id: int) -> bool:
    """:func:`_batch`, with any failure stopping the apply as interrupted.
    Never raises an ``Exception``."""
    try:
        return _batch(scan_id)
    except BaseException as exc:  # noqa: BLE001 — every failure stops the apply
        _log.error("Photo apply %s failed: %s", scan_id, _describe(exc))
        try:
            mark_apply_interrupted(scan_id, STOPPED_INTERRUPTED)
        except Exception:  # noqa: BLE001
            _log.exception("Could not stop photo apply %s", scan_id)
        if not isinstance(exc, Exception):
            raise  # KeyboardInterrupt / SystemExit: let the process go
        return False


def _enqueue_next(scan_id: int) -> None:
    # Named at its call, never looked up: tests/test_jobs_never_call_strava.py
    # traces every queued callable to its module.
    if not enqueue(QUEUE_PHOTOS, run_apply_batch, scan_id, max_retries=0, allow_inline=False):
        if _stop(scan_id, ("pending",), STOPPED_INTERRUPTED):
            _log.info("Photo apply %s stopped: could not be queued", scan_id)


def run_apply_batch(scan_id: int) -> None:
    """Job: one batch, then enqueue the next. Never raises."""
    if _run_batch(scan_id):
        _enqueue_next(scan_id)


def start_apply(scan_id: int) -> None:
    """Start, or resume, the apply of a ``ready`` scan's selected rows.

    Moves ``apply_status`` to ``pending`` from ``idle``, ``stopped`` or
    ``done`` (a stopped apply resumes with the rows still ``proposed``); does
    nothing while one is ``running``. With a broker, queues the first batch on
    ``photos``, and stops the apply as interrupted when it cannot. Without
    one, runs every batch here, in this call: a caller in a request passes it
    to its background tasks.
    """
    if not _cas_apply(scan_id, _STARTABLE, DBPhotoUpgradeScan.status == "ready",
                      apply_status="pending", apply_stopped_reason=None,
                      apply_heartbeat_at=time.time()):
        _log.info("Photo apply %s not started: scan not ready or apply running", scan_id)
        return
    if queue_available():
        _enqueue_next(scan_id)
        return
    while _run_batch(scan_id):
        pass


# ── Interruptions ─────────────────────────────────────────────────────────────

def recover_interrupted_apply(scan_id: int) -> None:
    """Settle each row of the scan left ``applying`` by a batch that died.

    The replace writes the photo list in one compare-and-set, so:

    * the old photo still listed: the replace never happened, ``proposed``;
    * otherwise ``applied``, when the list holds a photo sourced from the
      row's asset that no other applied row of the scan names (exact copies
      share an asset, D7);
    * otherwise ``proposed``: the photo went another way, and the next batch
      finds it gone.

    Call it only while no batch of this apply runs.
    """
    settled = {"applied": 0, "proposed": 0}
    with get_session() as sess:
        rows = sess.exec(select(DBPhotoUpgradeSuggestion).where(
            DBPhotoUpgradeSuggestion.scan_id == scan_id,
            DBPhotoUpgradeSuggestion.state == "applying")).all()
        for row in rows:
            model = DBMemory if row.artifact_kind == "memory" else DBJournalEntry
            artifact = sess.exec(select(model.photos_json, model.photo_order_json)
                                 .where(model.id == row.artifact_id)).first()
            state, applied_uuid = "proposed", None
            if artifact is not None:
                photos = [p for p in json.loads(artifact.photos_json or "[]") if p]
                sources = load_state(artifact.photo_order_json)["sources"]
                if row.photo_uuid not in photos:
                    claimed = set(sess.exec(
                        select(DBPhotoUpgradeSuggestion.applied_uuid).where(
                            DBPhotoUpgradeSuggestion.scan_id == scan_id,
                            DBPhotoUpgradeSuggestion.artifact_kind == row.artifact_kind,
                            DBPhotoUpgradeSuggestion.artifact_id == row.artifact_id,
                            DBPhotoUpgradeSuggestion.state == "applied",
                            DBPhotoUpgradeSuggestion.id != row.id)).all())
                    found = [p for p in photos if p not in claimed
                             and sources.get(p) == f"immich:{row.asset_id}"]
                    if found:
                        state, applied_uuid = "applied", found[0]
            done = sess.execute(
                update(DBPhotoUpgradeSuggestion)
                .where(DBPhotoUpgradeSuggestion.id == row.id,
                       DBPhotoUpgradeSuggestion.state == "applying")
                .values(state=state, applied_uuid=applied_uuid)
                .execution_options(synchronize_session=False))
            if done.rowcount == 1:
                settled[state] += 1
        sess.commit()
    if settled["applied"] or settled["proposed"]:
        _log.info("Photo apply %s: recovered %d rows as applied, %d as proposed",
                  scan_id, settled["applied"], settled["proposed"])


def mark_apply_interrupted(scan_id: int, reason: str) -> None:
    """Stop a ``pending`` or ``running`` apply whose job died where the chain
    could not see it (killed work-horse, dead worker, stale heartbeat), once
    its ``applying`` rows are recovered. The stop reason is always the code
    ``interrupted``, which the app words itself; *reason*, the caller's
    sentence, is not stored. No-op on any other state."""
    recover_interrupted_apply(scan_id)
    if _stop(scan_id, IN_PROGRESS, STOPPED_INTERRUPTED):
        _log.info("Photo apply %s stopped: interrupted", scan_id)


def sweep_running_applies() -> int:
    """Stop every ``running`` apply as interrupted. Called by the ``photos``
    worker as it starts, before it takes a job: it is the queue's only
    consumer, so no batch runs right now. ``pending`` applies are left alone:
    their job may still be waiting in Redis. Never raises."""
    try:
        with get_session() as sess:
            ids = list(sess.exec(select(DBPhotoUpgradeScan.id).where(
                DBPhotoUpgradeScan.apply_status == "running")).all())
    except Exception:  # noqa: BLE001 — a broken sweep must not stop the worker
        _log.exception("Photo apply startup sweep could not read scans")
        return 0
    stopped = 0
    for scan_id in ids:
        try:
            recover_interrupted_apply(scan_id)
            if _stop(scan_id, ("running",), STOPPED_INTERRUPTED):
                stopped += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            _log.exception("Could not stop photo apply %s", scan_id)
    if stopped:
        _log.info("Stopped %d photo apply(s) left running by a previous worker", stopped)
    return stopped
