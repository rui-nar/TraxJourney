"""REST routes of the Immich album photo upgrade (issue #33, U10 of
docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md; D2, D3, D5, D8, D9, D12).

Routes:
    POST   /api/projects/{name}/photo-upgrade/scans          — start a scan (202)
    GET    /api/projects/{name}/photo-upgrade/scans/latest   — the review and any newer scan

    GET    /api/photo-upgrade/scans/{id}/suggestions         — one page of the review
    PATCH  /api/photo-upgrade/scans/{id}/suggestions/{sid}   — tick, choose, reject
    POST   /api/photo-upgrade/scans/{id}/select              — tick in bulk
    GET    /api/photo-upgrade/scans/{id}/candidates          — a day's album photos, ranked
    POST   /api/photo-upgrade/scans/{id}/apply               — start or resume the apply (202)
    DELETE /api/photo-upgrade/scans/{id}                     — discard the scan

A scan is its runner's: every scan route answers 404 for a scan that is not
the caller's, as for one that does not exist. Who may scan what is D3:
memories for the trip owner only, journal entries for any editor or more,
their own entries only (the jobs re-check both).

Each person has at most one scan or apply in progress per trip. A scan
cannot start while the caller has a scan active or an apply ``pending`` or
``running`` on the trip, and an apply cannot start while the caller has a
scan active or another apply in progress on it. Two requests passing the
scan check at once are told apart by the partial unique index: the loser
answers the same 409. Two passing the apply check at once are told apart by
the conditional UPDATE that starts the apply.

Decisions on rows follow the rejection contract of
``src/photo_upgrade/scan_job.py``: a reject sets the row ``rejected`` and
records the pair on the scan; an un-reject, or choosing an asset for a photo,
takes the pair off. Every row change is a compare-and-set on the row as it
was read, so a row the apply took meanwhile answers 409 rather than being
overwritten.

The hourly :func:`sweep_photo_upgrade_scans` is registered in ``api/router.py``.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import time
from typing import Annotated, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, func, or_, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import select

from api.deps import get_current_user
from api.photo_replace import _KINDS, _data_dir
from api.project_access import OwnerParam, effective_role, require_role, resolve_project
from models.db import get_session
from models.project_db import (
    DBJournalEntry,
    DBMemory,
    DBPhotoUpgradeAsset,
    DBPhotoUpgradeScan,
    DBPhotoUpgradeSuggestion,
    DBProject,
    delete_photo_upgrade_scans,
)
from src.billing.entitlements import limits_for_user, quotas_enforced, storage_used
from src.jobs.queue import QUEUE_PHOTOS, queue_available, queue_has_workers
from src.photo_upgrade.apply_job import (
    STOPPED_INTERRUPTED,
    recover_interrupted_apply,
    start_apply,
)
from src.photo_upgrade.hashing import fingerprint
from src.photo_upgrade.matching import TripPhoto, rank_candidates
from src.photo_upgrade.scan_job import (
    REASON_INTERRUPTED,
    _album_photo,
    _original_size,
    _upright_size,
    photo_key,
    record_rejection,
    remove_rejection,
    start_scan,
)
from src.utils.logging import get_logger
from src.utils.photo_paths import photo_file, photo_folder

router = APIRouter(prefix="/api/projects", tags=["photo-upgrade"])
scans_router = APIRouter(prefix="/api/photo-upgrade/scans", tags=["photo-upgrade"])

_log = get_logger(__name__)

ACTIVE = ("pending", "scanning")
APPLY_IN_PROGRESS = ("pending", "running")
#: Where a POST /apply may start or resume from.
_APPLY_STARTABLE = ("idle", "stopped", "done")
STATES = ("proposed", "no_match", "already_original", "rejected", "applying", "applied",
          "failed", "stale")
#: Review rows per page.
PAGE_SIZE = 100
#: Days around a row's date an album photo may be chosen from (D7).
_DAY_SLACK = dt.timedelta(days=1)
_MEMBER_ROLES = ("owner", "co-owner", "editor")

# The hourly sweep (D8, D9).
RETENTION_S = 30 * 24 * 3600
STALE_PENDING_S = 24 * 3600
STALE_HEARTBEAT_S = 30 * 60
REASON_NEVER_STARTED = ("The check of your album did not start in time. "
                        "Please start it again.")

NO_WORKER = "No worker is processing photo jobs"

Filter = Literal["", "all", "to_check", "confident", "proposed", "no_match",
                 "already_original", "rejected", "applying", "applied", "failed", "stale"]


# ── Request/response schemas ──────────────────────────────────────────────────

class ScanRequest(BaseModel):
    album_id: str = Field(min_length=1, max_length=200)
    album_name: Optional[str] = Field(None, max_length=500)
    include_memories: bool = False
    include_journal: bool = False


class StorageOut(BaseModel):
    used: int = Field(description="Bytes counted against the caller")
    limit: Optional[int] = Field(None, description="null = unlimited")


class SummaryOut(BaseModel):
    counts: Dict[str, int] = Field(description="Rows per state, every state listed")
    confidence: Dict[str, int] = Field(
        description="`proposed` rows per confidence: `high` (confident) and `low` "
                    "(to check, a null confidence included)")
    selected: int = Field(description="Ticked `proposed` rows: what an apply would replace")
    selected_bytes_delta: int = Field(
        description="Sum of asset_bytes − current_bytes over those rows (unknown sizes as 0)")
    storage: StorageOut


class ScanOut(BaseModel):
    id: int
    album_id: str
    album_name: Optional[str]
    include_memories: bool
    include_journal: bool
    status: str = Field(description="pending | scanning | ready | failed")
    stage: Optional[str] = Field(None, description="reading_album | fingerprinting | "
                                                   "matching | done")
    progress_done: int
    progress_total: int
    error_message: Optional[str] = Field(None, description="A sentence to show, when failed")
    apply_status: str = Field(description="idle | pending | running | done | stopped")
    apply_stopped_reason: Optional[str] = Field(None, description="storage_full | interrupted")
    created_at: float
    completed_at: Optional[float]


class ReadyScanOut(ScanOut):
    summary: SummaryOut


class PhotoCountsOut(BaseModel):
    memories: Optional[int] = Field(
        None, description="Photos in the trip's memories; null unless the caller is the owner")
    journal: int = Field(description="Photos in the caller's own journal entries")


class LatestOut(BaseModel):
    scan: Optional[ReadyScanOut] = Field(None, description="The caller's newest ready scan")
    newer: Optional[ScanOut] = Field(
        None, description="The caller's newest scan, when it is newer and not ready")
    photo_counts: PhotoCountsOut


class AlternativeOut(BaseModel):
    asset_id: str
    score: int


class SuggestionOut(BaseModel):
    id: int
    artifact_kind: str = Field(description="memory | journal")
    artifact_id: int
    photo_uuid: str = Field(description="The trip photo this row replaces")
    current_uuid: str = Field(
        description="The photo listed now: applied_uuid once applied, else photo_uuid")
    position: int
    date: str
    state: str
    confidence: Optional[str] = Field(None, description="high | low")
    selected: bool
    asset_id: Optional[str]
    score: Optional[int]
    alternatives: List[AlternativeOut]
    asset_bytes: Optional[int]
    asset_width: Optional[int]
    asset_height: Optional[int]
    asset_mime: Optional[str]
    current_bytes: Optional[int]
    current_width: Optional[int]
    current_height: Optional[int]
    error_code: Optional[str]
    applied_uuid: Optional[str]


class SuggestionPageOut(BaseModel):
    items: List[SuggestionOut]
    next_cursor: Optional[str] = Field(None, description="null on the last page")


class SuggestionPatch(BaseModel):
    selected: Optional[bool] = None
    asset_id: Optional[str] = Field(None, min_length=1, max_length=200)
    rejected: Optional[bool] = None


class SuggestionChangedOut(BaseModel):
    suggestion: SuggestionOut
    summary: SummaryOut


class SelectRequest(BaseModel):
    which: Literal["confident", "all_proposed", "none"]


class CandidateOut(BaseModel):
    asset_id: str
    local_date: str
    score: Optional[int] = Field(None, description="null when the trip photo has no "
                                                   "fingerprint and nothing is ranked")
    mime: Optional[str]
    bytes: Optional[int]
    width: Optional[int]
    height: Optional[int]


class CandidatesOut(BaseModel):
    candidates: List[CandidateOut]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT,
                         detail={"code": code, "message": message})


def _ensure_worker() -> None:
    """503 when a broker is there but nothing consumes ``photos`` (D9)."""
    if queue_available() and not queue_has_workers(QUEUE_PHOTOS):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=NO_WORKER)


def _mine(project_id: int, user_info_id: int):
    return (DBPhotoUpgradeScan.project_id == project_id,
            DBPhotoUpgradeScan.user_info_id == user_info_id)


def _refuse_if_busy(sess, project_id: int, user_info_id: int) -> None:
    """409 while the caller has a scan active, or an apply in progress, on
    this trip. A second request can pass this at the same time: the scan
    insert's unique index answers it."""
    rows = sess.exec(select(DBPhotoUpgradeScan.status, DBPhotoUpgradeScan.apply_status)
                     .where(*_mine(project_id, user_info_id))).all()
    if any(s in ACTIVE for s, _ in rows):
        raise _scan_in_progress()
    if any(a in APPLY_IN_PROGRESS for _, a in rows):
        raise _apply_in_progress()


def _scan_in_progress() -> HTTPException:
    return _conflict("scan_in_progress", "A check of your album is already running on this trip.")


def _apply_in_progress() -> HTTPException:
    return _conflict("apply_in_progress", "Photos are being replaced on this trip.")


def _scan_out(scan: DBPhotoUpgradeScan) -> dict:
    return {k: getattr(scan, k) for k in ScanOut.model_fields}


def _storage(sess, user_info_id: int) -> dict:
    limit = (limits_for_user(sess, user_info_id).max_storage_bytes
             if quotas_enforced() else None)
    return {"used": storage_used(sess, user_info_id), "limit": limit}


def _summary(sess, scan: DBPhotoUpgradeScan) -> dict:
    S = DBPhotoUpgradeSuggestion
    counts = {state: 0 for state in STATES}
    for state, n in sess.exec(select(S.state, func.count()).where(S.scan_id == scan.id)
                              .group_by(S.state)).all():
        counts[state] = n
    high = sess.exec(select(func.count()).where(
        S.scan_id == scan.id, S.state == "proposed", S.confidence == "high")).one()
    selected, delta = sess.exec(select(
        func.count(), func.coalesce(func.sum(func.coalesce(S.asset_bytes, 0)
                                             - func.coalesce(S.current_bytes, 0)), 0))
        .where(S.scan_id == scan.id, S.state == "proposed", S.selected == True,  # noqa: E712
               S.asset_id.is_not(None))).one()
    return {"counts": counts,
            "confidence": {"high": high, "low": counts["proposed"] - high},
            "selected": selected, "selected_bytes_delta": int(delta),
            "storage": _storage(sess, scan.user_info_id)}


def _suggestion_out(row: DBPhotoUpgradeSuggestion) -> dict:
    try:
        alternatives = [a for a in json.loads(row.alternatives_json or "[]")
                        if isinstance(a, dict)]
    except ValueError:
        alternatives = []
    out = {k: getattr(row, k) for k in SuggestionOut.model_fields
           if k not in ("alternatives", "current_uuid")}
    out["alternatives"] = alternatives
    out["current_uuid"] = row.applied_uuid if row.state == "applied" and row.applied_uuid \
        else row.photo_uuid
    return out


def _owned_scan(sess, scan_id: int, caller: int, *, need_access: bool = True
                ) -> DBPhotoUpgradeScan:
    """The caller's scan; 404 for anyone else's or none. With *need_access*,
    the caller must still be a member of its trip (404 when no longer one, as
    for a stranger) with editor access (403)."""
    scan = sess.get(DBPhotoUpgradeScan, scan_id)
    if scan is None or scan.user_info_id != caller:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
    if need_access:
        project = sess.get(DBProject, scan.project_id)
        role = effective_role(sess, project, caller) if project is not None else None
        if role is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
        if role not in _MEMBER_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="You need editor access to do this")
    return scan


def _owned_row(sess, scan: DBPhotoUpgradeScan, suggestion_id: int) -> DBPhotoUpgradeSuggestion:
    row = sess.get(DBPhotoUpgradeSuggestion, suggestion_id)
    if row is None or row.scan_id != scan.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Suggestion not found")
    return row


def _day_window(day: str) -> tuple[str, str]:
    d = dt.date.fromisoformat(day)
    return (d - _DAY_SLACK).isoformat(), (d + _DAY_SLACK).isoformat()


def _folder(scan: DBPhotoUpgradeScan, project: DBProject, kind: str, artifact_id: int):
    """Where a row's photo is stored: a memory's in the owner's tree, a
    journal entry's in the runner's (D3, as the apply writes them)."""
    owner_dir = project.user_info_id if kind == "memory" else scan.user_info_id
    return photo_folder(_data_dir(kind), owner_dir, _KINDS[kind][1], artifact_id)


def _listed(sess, kind: str, artifact_id: int) -> List[str]:
    model = _KINDS[kind][0]
    raw = sess.exec(select(model.photos_json).where(model.id == artifact_id)).first()
    return [p for p in json.loads(raw or "[]") if p] if raw is not None else []


def _encode_cursor(row: DBPhotoUpgradeSuggestion) -> str:
    key = [row.date, row.artifact_kind, row.artifact_id, row.position, row.id]
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> list:
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error):
        key = None
    if (not isinstance(key, list) or len(key) != 5
            or not isinstance(key[0], str) or not isinstance(key[1], str)
            or not all(isinstance(v, int) for v in key[2:])):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Invalid cursor")
    return key


def _filter_clause(name: str):
    S = DBPhotoUpgradeSuggestion
    if name in ("", "all"):
        return None
    if name == "to_check":
        return and_(S.state == "proposed", or_(S.confidence.is_(None), S.confidence != "high"))
    if name == "confident":
        return and_(S.state == "proposed", S.confidence == "high")
    return S.state == name


# ── Project routes ────────────────────────────────────────────────────────────

@router.post("/{name}/photo-upgrade/scans", status_code=status.HTTP_202_ACCEPTED,
             response_model=ScanOut, summary="Start checking an Immich album against the trip")
def create_photo_upgrade_scan(
    name: str,
    body: ScanRequest,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    """Insert a ``pending`` scan and start its job chain.

    503 with no ``photos`` worker → 422 with nothing to check → 404/403 by
    role (editor or more; memories need the owner, D3) → 409 while the caller
    has a scan or an apply in progress on the trip, also when a concurrent
    request inserted its scan first.
    """
    caller = int(current_user["sub"])
    _ensure_worker()
    if not (body.include_memories or body.include_journal):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Choose memories, journal entries or both")
    with get_session() as sess:
        row = resolve_project(sess, caller, name, owner, min_role="editor")
        if body.include_memories:
            require_role(sess, row, caller, "owner")
        project_id = row.id
        _refuse_if_busy(sess, project_id, caller)
        scan = DBPhotoUpgradeScan(project_id=project_id, user_info_id=caller,
                                  album_id=body.album_id, album_name=body.album_name,
                                  include_memories=body.include_memories,
                                  include_journal=body.include_journal, status="pending")
        sess.add(scan)
        try:
            sess.commit()
        except IntegrityError:
            # uq_photoupgradescan_active: a request that passed the check at
            # the same time (a double tap) inserted its scan first (R2-3).
            sess.rollback()
            raise _scan_in_progress() from None
        sess.refresh(scan)
        out = _scan_out(scan)
    background_tasks.add_task(start_scan, out["id"])
    _log.info("Photo scan %s created on project %s", out["id"], project_id)
    return out


@router.get("/{name}/photo-upgrade/scans/latest", response_model=LatestOut,
            summary="The caller's latest photo-upgrade review, and any newer scan")
def get_latest_photo_upgrade_scan(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    owner: OwnerParam = None,
):
    caller = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, caller, name, owner, min_role="editor")
        order = (DBPhotoUpgradeScan.created_at.desc(), DBPhotoUpgradeScan.id.desc())
        ready = sess.exec(select(DBPhotoUpgradeScan)
                          .where(*_mine(row.id, caller), DBPhotoUpgradeScan.status == "ready")
                          .order_by(*order)).first()
        newest = sess.exec(select(DBPhotoUpgradeScan).where(*_mine(row.id, caller))
                           .order_by(*order)).first()
        scan_out = None
        if ready is not None:
            scan_out = {**_scan_out(ready), "summary": _summary(sess, ready)}
        newer = None
        if newest is not None and newest.status != "ready" and (
                ready is None or newest.id != ready.id):
            newer = _scan_out(newest)
        return {"scan": scan_out, "newer": newer,
                "photo_counts": _photo_counts(sess, row, caller)}


def _photo_counts(sess, project: DBProject, caller: int) -> dict:
    """What each checkbox would cover (D3); reads photo lists only."""
    def count(model, *where) -> int:
        return sum(len([p for p in json.loads(raw or "[]") if p]) for raw in sess.exec(
            select(model.photos_json).where(model.project_id == project.id, *where)).all())

    mine = DBJournalEntry.user_info_id == caller
    if caller == project.user_info_id:
        mine = or_(mine, DBJournalEntry.user_info_id.is_(None))
    return {"memories": count(DBMemory) if caller == project.user_info_id else None,
            "journal": count(DBJournalEntry, mine)}


# ── Scan routes ───────────────────────────────────────────────────────────────

@scans_router.get("/{scan_id}/suggestions", response_model=SuggestionPageOut,
                  summary="One page of a scan's review, in trip order")
def list_photo_upgrade_suggestions(
    scan_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    filter: Filter = "",
    cursor: Optional[str] = None,
):
    """``PAGE_SIZE`` rows ordered by date, kind, artifact and position.
    ``filter`` is a state, or ``to_check`` / ``confident`` (``proposed`` rows
    by confidence). Pass ``next_cursor`` back for the next page."""
    caller = int(current_user["sub"])
    S = DBPhotoUpgradeSuggestion
    order = (S.date, S.artifact_kind, S.artifact_id, S.position, S.id)
    with get_session() as sess:
        scan = _owned_scan(sess, scan_id, caller)
        query = select(S).where(S.scan_id == scan.id)
        clause = _filter_clause(filter)
        if clause is not None:
            query = query.where(clause)
        if cursor:
            query = query.where(tuple_(*order) > tuple_(*_decode_cursor(cursor)))
        rows = sess.exec(query.order_by(*order).limit(PAGE_SIZE + 1)).all()
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        return {"items": [_suggestion_out(r) for r in rows],
                "next_cursor": _encode_cursor(rows[-1]) if more else None}


@scans_router.patch("/{scan_id}/suggestions/{suggestion_id}",
                    response_model=SuggestionChangedOut,
                    summary="Tick, choose another album photo for, or reject one row")
def update_photo_upgrade_suggestion(
    scan_id: int,
    suggestion_id: int,
    body: SuggestionPatch,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Change one row, by compare-and-set on the row as read (409 when the
    apply or another request changed it meanwhile).

    * ``asset_id``: one of the scan's album photos dated within the row's
      date ±1 day (422 otherwise). The row becomes ``proposed``, confident
      and ticked (unless ``selected`` says otherwise). On an ``applied`` row
      it replaces the original the apply stored (D5): ``photo_uuid`` becomes
      that ``applied_uuid``, which must still be listed, and ``applied_uuid``
      is cleared, since the apply and its recovery act on ``photo_uuid``
      (U11-R1-4). The pair is taken off the scan's rejections.
    * ``rejected: true``: a ``proposed`` or ``failed`` row with an asset
      becomes ``rejected`` and unticked, and the pair is recorded on the scan.
      ``rejected: false`` puts a ``rejected`` row back to ``proposed`` and
      takes the pair off.
    * ``selected``: ticks or unticks; only a ``proposed`` row can be ticked.

    Rows ``applying`` or ``stale`` cannot be changed.
    """
    caller = int(current_user["sub"])
    if body.selected is None and body.asset_id is None and body.rejected is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Nothing to change")
    if body.rejected and (body.asset_id is not None or body.selected):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="A rejected photo cannot be chosen or ticked")
    S = DBPhotoUpgradeSuggestion
    with get_session() as sess:
        scan = _owned_scan(sess, scan_id, caller)
        row = _owned_row(sess, scan, suggestion_id)
        if row.state in ("applying", "stale"):
            raise _conflict("suggestion_locked", "This photo cannot be changed now.")

        values: dict = {}
        state = row.state
        photo_uuid = row.photo_uuid
        # (key, asset id) pairs to record on, or take off, the scan's rejections.
        record: Optional[tuple] = None
        lift: Optional[tuple] = None

        if body.asset_id is not None and body.asset_id != row.asset_id:
            if state not in ("proposed", "no_match", "already_original", "rejected",
                             "failed", "applied"):
                raise _conflict("suggestion_locked", "This photo cannot be changed now.")
            lo, hi = _day_window(row.date)
            asset = sess.exec(select(DBPhotoUpgradeAsset).where(
                DBPhotoUpgradeAsset.scan_id == scan.id,
                DBPhotoUpgradeAsset.asset_id == body.asset_id,
                DBPhotoUpgradeAsset.local_date >= lo,
                DBPhotoUpgradeAsset.local_date <= hi)).first()
            if asset is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="This album photo is not one of the scan's photos from "
                           "that day or the days around it")
            width, height = _original_size(asset)
            values.update(asset_id=asset.asset_id, asset_bytes=asset.file_bytes,
                          asset_width=width, asset_height=height, asset_mime=asset.mime,
                          score=_alternative_score(row, asset.asset_id),
                          confidence="high", error_code=None)
            if state == "applied":
                # D5 "Choose another": the original the apply stored is the
                # photo this row now replaces (U11-R1-4).
                project = sess.get(DBProject, scan.project_id)
                if (not row.applied_uuid or project is None
                        or row.applied_uuid not in _listed(sess, row.artifact_kind,
                                                           row.artifact_id)):
                    raise _conflict("photo_gone", "This photo is no longer in the trip.")
                photo_uuid = row.applied_uuid
                values.update(photo_uuid=photo_uuid, applied_uuid=None,
                              **_current_size(_folder(scan, project, row.artifact_kind,
                                                      row.artifact_id), photo_uuid))
            state = "proposed"
            values["state"] = state
            values["selected"] = True if body.selected is None else body.selected
            lift = (photo_key(row.artifact_kind, row.artifact_id, photo_uuid), asset.asset_id)
        elif body.rejected is True and state != "rejected":
            if state not in ("proposed", "failed") or row.asset_id is None:
                raise _conflict("not_rejectable", "Only a proposed photo can be turned down.")
            state = "rejected"
            values.update(state=state, selected=False, error_code=None)
            record = (photo_key(row.artifact_kind, row.artifact_id, photo_uuid), row.asset_id)
        elif body.rejected is False and state == "rejected" and row.asset_id is not None:
            state = "proposed"
            values["state"] = state
            lift = (photo_key(row.artifact_kind, row.artifact_id, photo_uuid), row.asset_id)
        elif (body.asset_id is not None and body.asset_id == row.asset_id
              and state == "rejected"):
            # Choosing back the very asset this row rejected is an un-reject.
            state = "proposed"
            values.update(state=state, selected=True if body.selected is None
                          else body.selected)
            lift = (photo_key(row.artifact_kind, row.artifact_id, photo_uuid), row.asset_id)

        if body.selected is not None and "selected" not in values:
            if body.selected and state != "proposed":
                raise _conflict("not_selectable", "Only a proposed photo can be ticked.")
            if body.selected != row.selected:
                values["selected"] = body.selected

        if values:
            done = sess.execute(
                update(S).where(S.id == row.id, S.state == row.state,
                                S.photo_uuid == row.photo_uuid,
                                S.asset_id.is_not_distinct_from(row.asset_id),
                                S.applied_uuid.is_not_distinct_from(row.applied_uuid),
                                S.selected == row.selected)
                .values(**values).execution_options(synchronize_session=False))
            if done.rowcount != 1:
                sess.rollback()
                raise _conflict("suggestion_changed",
                                "This photo changed meanwhile. Reload and try again.")
            sess.commit()
    # After the row: the row alone already says what the scan holds for this
    # pair (scan_job._earlier_rejections), so a lost column edit loses nothing.
    if record is not None:
        record_rejection(scan_id, *record)
    if lift is not None:
        remove_rejection(scan_id, *lift)
    with get_session() as sess:
        scan = sess.get(DBPhotoUpgradeScan, scan_id)
        row = sess.get(S, suggestion_id)
        if scan is None or row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
        return {"suggestion": _suggestion_out(row), "summary": _summary(sess, scan)}


def _alternative_score(row: DBPhotoUpgradeSuggestion, asset_id: str) -> Optional[int]:
    """The matcher's score for *asset_id* on this photo, when it kept one."""
    try:
        alternatives = json.loads(row.alternatives_json or "[]")
    except ValueError:
        return None
    for alt in alternatives:
        if isinstance(alt, dict) and alt.get("asset_id") == asset_id:
            score = alt.get("score")
            return score if isinstance(score, int) else None
    return None


def _current_size(folder, uuid: str) -> dict:
    """``current_*`` for the photo stored as *uuid*, from its file."""
    full = photo_file(folder, uuid)
    size = None
    try:
        nbytes = full.stat().st_size if full is not None else None
    except OSError:
        nbytes = None
    if nbytes is not None:
        size = _upright_size(full)
    return {"current_bytes": nbytes,
            "current_width": size[0] if size else None,
            "current_height": size[1] if size else None}


@scans_router.post("/{scan_id}/select", response_model=SummaryOut,
                   summary="Tick the confident rows, every proposed row, or none")
def select_photo_upgrade_suggestions(
    scan_id: int,
    body: SelectRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """``confident`` and ``all_proposed`` tick those ``proposed`` rows and
    leave the others as they are; ``none`` unticks every row."""
    caller = int(current_user["sub"])
    S = DBPhotoUpgradeSuggestion
    with get_session() as sess:
        scan = _owned_scan(sess, scan_id, caller)
        if body.which == "none":
            stmt = update(S).where(S.scan_id == scan.id, S.selected == True  # noqa: E712
                                   ).values(selected=False)
        else:
            where = [S.scan_id == scan.id, S.state == "proposed", S.asset_id.is_not(None)]
            if body.which == "confident":
                where.append(S.confidence == "high")
            stmt = update(S).where(*where).values(selected=True)
        sess.execute(stmt.execution_options(synchronize_session=False))
        sess.commit()
        return _summary(sess, scan)


@scans_router.get("/{scan_id}/candidates", response_model=CandidatesOut,
                  summary="The scan's album photos from a row's day, best match first")
def list_photo_upgrade_candidates(
    scan_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    suggestion_id: int = Query(...),
):
    """For "Choose another": the scan's fingerprinted album photos dated
    within the row's date ±1 day, ranked by ``rank_candidates`` against the
    photo listed now. When that photo has no readable thumbnail nothing can
    be ranked, and every one of them is listed by date, without a score."""
    caller = int(current_user["sub"])
    with get_session() as sess:
        scan = _owned_scan(sess, scan_id, caller)
        row = _owned_row(sess, scan, suggestion_id)
        project = sess.get(DBProject, scan.project_id)
        model = _KINDS[row.artifact_kind][0]
        place = sess.exec(select(model.lat, model.lon)
                          .where(model.id == row.artifact_id)).first()
        lo, hi = _day_window(row.date)
        assets = sess.exec(select(DBPhotoUpgradeAsset).where(
            DBPhotoUpgradeAsset.scan_id == scan.id,
            DBPhotoUpgradeAsset.fingerprinted == True,  # noqa: E712
            DBPhotoUpgradeAsset.local_date >= lo,
            DBPhotoUpgradeAsset.local_date <= hi)
            .order_by(DBPhotoUpgradeAsset.local_date, DBPhotoUpgradeAsset.id)).all()

    uuid = row.applied_uuid if row.state == "applied" and row.applied_uuid else row.photo_uuid
    fp = None
    thumb = photo_file(_folder(scan, project, row.artifact_kind, row.artifact_id),
                       uuid, "_thumb") if project is not None else None
    if thumb is not None:
        try:
            fp = fingerprint(thumb.read_bytes())
        except OSError:
            fp = None
    by_id = {a.asset_id: a for a in assets}
    if fp is not None:
        photo = TripPhoto(key=photo_key(row.artifact_kind, row.artifact_id, uuid),
                          date=dt.date.fromisoformat(row.date),
                          lat=place.lat if place else None, lon=place.lon if place else None,
                          fp=fp, pixels=None, source=None, content_hash=None)
        ranked = rank_candidates(photo, [_album_photo(a) for a in assets])
    else:
        ranked = [(a.asset_id, None) for a in assets]
    out = []
    for asset_id, score in ranked:
        a = by_id[asset_id]
        width, height = _original_size(a)
        out.append({"asset_id": a.asset_id, "local_date": a.local_date, "score": score,
                    "mime": a.mime, "bytes": a.file_bytes, "width": width, "height": height})
    return {"candidates": out}


@scans_router.post("/{scan_id}/apply", status_code=status.HTTP_202_ACCEPTED,
                   response_model=ScanOut, summary="Replace the ticked photos, or resume")
def apply_photo_upgrade_scan(
    scan_id: int,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Set the apply ``pending`` and start its batches. From ``idle``,
    ``stopped`` (a resume) or ``done``; 409 while it is ``pending`` or
    ``running``, while the caller has a scan in progress on the trip, or an
    apply in progress on another of their scans of it (R2-2)."""
    caller = int(current_user["sub"])
    _ensure_worker()
    with get_session() as sess:
        scan = _owned_scan(sess, scan_id, caller)
        if scan.status != "ready":
            raise _conflict("scan_not_ready", "This check of your album is not finished.")
        if scan.apply_status in APPLY_IN_PROGRESS:
            raise _apply_in_progress()
        _refuse_if_busy(sess, scan.project_id, caller)
        # The same rule as one UPDATE, so two requests passing the reads
        # above at once start one apply between them.
        other = aliased(DBPhotoUpgradeScan)
        busy = exists().where(
            other.project_id == scan.project_id, other.user_info_id == caller,
            or_(other.status.in_(ACTIVE),
                and_(other.id != scan.id, other.apply_status.in_(APPLY_IN_PROGRESS))))
        started = sess.execute(
            update(DBPhotoUpgradeScan)
            .where(DBPhotoUpgradeScan.id == scan.id, DBPhotoUpgradeScan.status == "ready",
                   DBPhotoUpgradeScan.apply_status.in_(_APPLY_STARTABLE), ~busy)
            .values(apply_status="pending", apply_stopped_reason=None,
                    apply_heartbeat_at=time.time())
            .execution_options(synchronize_session=False))
        if started.rowcount != 1:
            sess.rollback()
            raise _apply_in_progress()
        sess.commit()
        sess.refresh(scan)
        out = _scan_out(scan)
    background_tasks.add_task(start_apply, scan_id)
    _log.info("Photo apply %s requested", scan_id)
    return out


@scans_router.delete("/{scan_id}", status_code=status.HTTP_204_NO_CONTENT,
                     summary="Discard a scan and its review")
def delete_photo_upgrade_scan(
    scan_id: int,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """409 while its apply is ``pending`` or ``running``. A scan still
    running stops at its next step. Allowed after the caller lost access to
    the trip, so they can still discard what they ran."""
    caller = int(current_user["sub"])
    with get_session() as sess:
        scan = _owned_scan(sess, scan_id, caller, need_access=False)
        gone = delete_photo_upgrade_scans(
            sess, DBPhotoUpgradeScan.id == scan.id,
            DBPhotoUpgradeScan.apply_status.not_in(APPLY_IN_PROGRESS))
        if gone != 1:
            sess.rollback()
            raise _apply_in_progress()
        sess.commit()
    return None


# ── Hourly sweep ──────────────────────────────────────────────────────────────

def sweep_photo_upgrade_scans(now: Optional[float] = None) -> dict:
    """The API's hourly photo-upgrade sweep (D8, D9). Never raises. Returns
    what it did.

    1. Fails scans still ``pending`` after ``STALE_PENDING_S`` (their queue
       entry is gone) and scans ``scanning`` whose heartbeat is older than
       ``STALE_HEARTBEAT_S`` (their job died unseen, ``interrupted``).
    2. Stops, as ``interrupted``, applies still ``pending`` after
       ``STALE_PENDING_S`` and applies ``running`` whose heartbeat is older
       than ``STALE_HEARTBEAT_S``, then recovers their ``applying`` rows. The
       stop comes first, conditioned on the heartbeat read, so a batch that
       beat meanwhile keeps running, and no batch can start while the
       recovery runs.
    3. Deletes scans created more than ``RETENTION_S`` ago, except one whose
       apply is in progress: step 2 stops it if it is stale, and a later
       sweep deletes it.
    """
    now = time.time() if now is None else now
    done = {"scans_failed": 0, "applies_stopped": 0, "scans_deleted": 0}
    Scan = DBPhotoUpgradeScan
    try:
        with get_session() as sess:
            done["scans_failed"] += sess.execute(
                update(Scan).where(Scan.status == "pending",
                                   Scan.created_at < now - STALE_PENDING_S)
                .values(status="failed", error_message=REASON_NEVER_STARTED,
                        completed_at=now)).rowcount
            done["scans_failed"] += sess.execute(
                update(Scan).where(
                    Scan.status == "scanning",
                    func.coalesce(Scan.heartbeat_at, Scan.created_at)
                    < now - STALE_HEARTBEAT_S)
                .values(status="failed", error_message=REASON_INTERRUPTED,
                        completed_at=now)).rowcount
            sess.commit()

        stale_apply = or_(
            and_(Scan.apply_status == "pending",
                 func.coalesce(Scan.apply_heartbeat_at, Scan.created_at)
                 < now - STALE_PENDING_S),
            and_(Scan.apply_status == "running",
                 func.coalesce(Scan.apply_heartbeat_at, Scan.created_at)
                 < now - STALE_HEARTBEAT_S))
        with get_session() as sess:
            ids = list(sess.exec(select(Scan.id).where(stale_apply)).all())
        for scan_id in ids:
            try:
                with get_session() as sess:
                    stopped = sess.execute(
                        update(Scan).where(Scan.id == scan_id, stale_apply)
                        .values(apply_status="stopped",
                                apply_stopped_reason=STOPPED_INTERRUPTED,
                                apply_heartbeat_at=now)).rowcount
                    sess.commit()
                if stopped:
                    recover_interrupted_apply(scan_id)
                    done["applies_stopped"] += 1
            except Exception:  # noqa: BLE001 — one bad scan must not stop the sweep
                _log.exception("Could not stop photo apply %s", scan_id)

        with get_session() as sess:
            done["scans_deleted"] = delete_photo_upgrade_scans(
                sess, Scan.created_at < now - RETENTION_S,
                Scan.apply_status.not_in(APPLY_IN_PROGRESS))
            sess.commit()
    except Exception:  # noqa: BLE001 — a scheduled job must not die loudly
        _log.exception("Photo upgrade sweep failed")
    if any(done.values()):
        _log.info("Photo upgrade sweep: %s", done)
    return done
