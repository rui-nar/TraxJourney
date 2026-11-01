"""The scan job chain: an Immich album and a trip become stored suggestions
(issue #33, U9 of docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md; D3, D7, D8, D9, D12).

A scan is a chain of short jobs on the ``photos`` queue (D9), each enqueuing
the next, so a poster never waits behind more than one of them:

1. ``run_scan_prepare``: ``pending → scanning``, counts the trip photos in
   scope (D3, at most ``MAX_TRIP_PHOTOS``) and stores one row per album photo
   dated within the trip ±1 day (D12).
2. ``run_scan_fingerprint``: fetches up to ``FINGERPRINT_BATCH`` album
   thumbnails, ``FETCH_THREADS`` at a time, each thread hashing what it
   fetched before the next, and stores their fingerprints;
   enqueues itself again while rows remain.
3. ``run_scan_match``: fingerprints the trip photos from their thumbnails on
   disk, runs the matcher (``src/photo_upgrade/matching.py``), writes one
   suggestion per trip photo, sets ``ready``, and only then deletes the
   person's earlier scans of the trip, except one whose apply is in progress
   (D8).

Without a broker, :func:`start_scan` runs the same steps in one loop in this
process. Every job first checks that its scan is still there and in the
state it left it in, and stops quietly otherwise: a deleted trip deletes its
scans. Every state change is a compare-and-set, as in
``src/video/job_runner.py``, because a scan can be failed by someone else
while a job runs:

* the killed-work-horse handler (``mark_scan_interrupted``, wired in
  ``src.jobs.worker``),
* the ``photos`` worker's startup sweep (``sweep_scanning_scans``),
* the API's hourly sweep (U10), on a stale ``heartbeat_at``.

``error_message`` only ever holds one of the sentences below, which the app
shows as they are. Failures are logged by exception type, never by message:
an Immich error can quote the server's address. The key is never logged.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from PIL import Image
from sqlalchemy import func, or_, update
from sqlmodel import select

from api.photo_order import load_state
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
from models.user import ImmichToken
from src.billing.trip_days import normalise, project_day_bounds
from src.immich.client import (
    REQUIRED_PERMISSIONS,
    ImmichAddressNotAllowed,
    ImmichBadAnswer,
    ImmichClient,
    ImmichError,
    ImmichRejected,
    ImmichTooManyAssets,
    ImmichUnreachable,
)
from src.jobs.queue import QUEUE_PHOTOS, enqueue, queue_available
from src.photo_upgrade.hashing import Fingerprint, fingerprint, from_hex, to_hex
from src.photo_upgrade.matching import (
    HIGH,
    NO_MATCH,
    PROPOSED,
    AlbumPhoto,
    TripPhoto,
    match,
)
from src.utils.logging import get_logger
from src.utils.photo_paths import photo_file, photo_folder

_log = get_logger(__name__)

#: Album photos fingerprinted per job (D9).
FINGERPRINT_BATCH = 250
#: Thumbnails fetched at once.
FETCH_THREADS = 4
#: Most trip photos one scan considers (D12).
MAX_TRIP_PHOTOS = 5000
#: Immich thumbnail size hashed: WebP, about 332×250 on v5.9.0 (OD1).
_THUMB_SIZE = "thumbnail"
#: Largest album thumbnail decoded. Immich's answers are untrusted (E2): a
#: "thumbnail" declaring more pixels than this is a failed fetch, never a
#: decode of up to Pillow's own, far larger, limit.
_THUMB_MAX_PIXELS = 4_000_000
#: Days around the trip an album photo may be dated (D7's ±1 day).
_DAY_SLACK = dt.timedelta(days=1)
#: Widest gap between local time and UTC, either way. Immich filters on the
#: UTC capture instant, so the window asked for is widened by these and the
#: local day of each answer decides.
_UTC_AHEAD = dt.timedelta(hours=14)
_UTC_BEHIND = dt.timedelta(hours=12)

ACTIVE = ("pending", "scanning")
_APPLY_IN_PROGRESS = ("pending", "running")

# Progress stages, mapped to words by the app.
STAGE_READING_ALBUM = "reading_album"
STAGE_FINGERPRINTING = "fingerprinting"
STAGE_MATCHING = "matching"
STAGE_DONE = "done"

# The only strings ever stored in ``error_message``.
REASON_INTERRUPTED = "The check of your album was interrupted. Please start it again."
REASON_UNAVAILABLE = "The photo service is unavailable. Please try again later."
REASON_FAILED = "The check of your album failed. Please try again."
REASON_TIMED_OUT = ("Your Immich server answered too slowly. Please try again "
                    "later.")
REASON_NOT_CONNECTED = "Immich is not connected. Connect it in Settings and try again."
REASON_NO_ACCESS = "You no longer have editor access to this trip."
REASON_NOT_OWNER = "Only the trip owner can upgrade memory photos."
REASON_UNREACHABLE = ("Could not reach your Immich server. Check that it is "
                      "online and try again.")
REASON_FORBIDDEN = ("Immich refused access. The API key needs the permissions "
                    f"{', '.join(REQUIRED_PERMISSIONS[:-1])} and "
                    f"{REQUIRED_PERMISSIONS[-1]}.")
REASON_ALBUM_NOT_FOUND = "This album was not found on your Immich server."
REASON_ADDRESS_NOT_ALLOWED = ("This Immich server's address is not allowed. A "
                              "server on a private network must be listed by "
                              "the operator.")
REASON_BAD_ANSWER = "Your Immich server sent an answer that could not be read."
REASON_TOO_MANY_TRIP_PHOTOS = (f"This trip has more than {MAX_TRIP_PHOTOS:,} photos "
                               "to check, more than one check can take.")


def _reason_too_many_album_photos(limit: int) -> str:
    return (f"This album has more than {limit:,} photos from the trip's dates. "
            "Choose a smaller album.")


class _ScanFailed(Exception):
    """Ends the chain and fails the scan with ``reason``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _describe(exc: BaseException) -> str:
    """*exc*'s type and traceback, without its message: an Immich error's
    message can quote the server's address."""
    return type(exc).__qualname__ + "\n" + "".join(traceback.format_tb(exc.__traceback__))


def _reason_for(exc: BaseException) -> str:
    if isinstance(exc, _ScanFailed):
        return exc.reason
    if isinstance(exc, ImmichTooManyAssets):
        return _reason_too_many_album_photos(exc.max_items)
    if isinstance(exc, ImmichUnreachable):
        return REASON_UNREACHABLE
    if isinstance(exc, ImmichRejected):
        if exc.status in (401, 403):
            return REASON_FORBIDDEN
        if exc.status == 404:
            return REASON_ALBUM_NOT_FOUND
        return f"Your Immich server refused the request (HTTP {exc.status})."
    if isinstance(exc, ImmichAddressNotAllowed):
        return REASON_ADDRESS_NOT_ALLOWED
    if isinstance(exc, ImmichBadAnswer):
        return REASON_BAD_ANSWER
    if type(exc).__name__ == "JobTimeoutException":  # RQ's job_timeout
        return REASON_TIMED_OUT
    return REASON_FAILED


# ── Row helpers ───────────────────────────────────────────────────────────────

def _cas(scan_id: int, from_states: Iterable[str], **values) -> bool:
    """Move the scan to *values* only if its status is one of *from_states*.
    True when this call made the change."""
    with get_session() as sess:
        result = sess.execute(
            update(DBPhotoUpgradeScan)
            .where(DBPhotoUpgradeScan.id == scan_id,
                   DBPhotoUpgradeScan.status.in_(tuple(from_states)))
            .values(**values)
        )
        sess.commit()
        return result.rowcount == 1


def _hold(sess, scan_id: int, **values) -> bool:
    """Inside *sess*: beat the heartbeat of a ``scanning`` scan, with
    *values*. The UPDATE takes SQLite's write lock, so rows written after it
    in the same transaction cannot outlive a scan deleted or failed
    meanwhile. False when the scan is no longer ``scanning``."""
    result = sess.execute(
        update(DBPhotoUpgradeScan)
        .where(DBPhotoUpgradeScan.id == scan_id, DBPhotoUpgradeScan.status == "scanning")
        .values(heartbeat_at=time.time(), **values)
    )
    return result.rowcount == 1


def _fail(scan_id: int, reason: str) -> bool:
    return _cas(scan_id, ACTIVE, status="failed", error_message=reason,
                completed_at=time.time())


def _live_scan(scan_id: int, status: str) -> Optional[DBPhotoUpgradeScan]:
    """The scan, when it and its trip still exist and it is *status*."""
    with get_session() as sess:
        scan = sess.get(DBPhotoUpgradeScan, scan_id)
        if scan is None or scan.status != status:
            return None
        if sess.get(DBProject, scan.project_id) is None:
            return None
        sess.expunge(scan)
        return scan


def _make_client(server_url: str, api_key: str) -> ImmichClient:
    """The runner's Immich client. Tests replace this function."""
    return ImmichClient(server_url, api_key)


def _client_for(user_info_id: int) -> ImmichClient:
    with get_session() as sess:
        tok = sess.exec(
            select(ImmichToken).where(ImmichToken.user_info_id == user_info_id)
        ).first()
        if tok is None or not tok.api_key or not tok.server_url:
            raise _ScanFailed(REASON_NOT_CONNECTED)
        server_url, api_key = tok.server_url, tok.api_key
    return _make_client(server_url, api_key)


# ── Trip photos in scope (D3) ─────────────────────────────────────────────────

class _Artifact:
    """One memory or journal entry in scope, with its live photo list. Built
    from the columns :func:`_in_scope` selects, never a whole row: a scan
    reads no memory or journal text."""

    def __init__(self, kind: str, row, owner_dir: int):
        self.kind = kind
        self.id = row.id
        self.date = row.date
        self.lat = row.lat
        self.lon = row.lon
        self.photos: List[str] = [p for p in json.loads(row.photos_json or "[]") if p]
        self.state = load_state(row.photo_order_json)
        self.owner_dir = owner_dir

    def folder(self) -> Path:
        if self.kind == "memory":
            import api.memories as module
            return photo_folder(module._DATA_DIR, self.owner_dir, "memories", self.id)
        import api.journal as module
        return photo_folder(module._DATA_DIR, self.owner_dir, "journal", self.id)


def _columns(model):
    return (model.id, model.date, model.lat, model.lon, model.photos_json,
            model.photo_order_json)


def _in_scope(sess, scan: DBPhotoUpgradeScan) -> List[_Artifact]:
    """The memories and journal entries this scan covers, roles re-checked
    (D3): memories for the owner only, journal entries their runner wrote (a
    NULL author is the owner). Raises ``_ScanFailed`` when the runner no
    longer has the access the scan asked for."""
    from api.project_access import effective_role

    project = sess.get(DBProject, scan.project_id)
    runner = scan.user_info_id
    role = effective_role(sess, project, runner)
    if role not in ("owner", "co-owner", "editor"):
        raise _ScanFailed(REASON_NO_ACCESS)
    if scan.include_memories and role != "owner":
        raise _ScanFailed(REASON_NOT_OWNER)

    owner = project.user_info_id
    found: List[_Artifact] = []
    if scan.include_memories:
        for row in sess.exec(select(*_columns(DBMemory))
                             .where(DBMemory.project_id == project.id)
                             .order_by(DBMemory.date, DBMemory.id)).all():
            found.append(_Artifact("memory", row, owner))
    if scan.include_journal:
        mine = DBJournalEntry.user_info_id == runner
        if runner == owner:
            mine = or_(mine, DBJournalEntry.user_info_id.is_(None))
        for row in sess.exec(select(*_columns(DBJournalEntry)).where(
                DBJournalEntry.project_id == project.id, mine)
                .order_by(DBJournalEntry.date, DBJournalEntry.id)).all():
            found.append(_Artifact("journal", row, runner))
    return found


def _check_trip_photo_count(artifacts: List[_Artifact]) -> int:
    count = sum(len(a.photos) for a in artifacts)
    if count > MAX_TRIP_PHOTOS:
        raise _ScanFailed(REASON_TOO_MANY_TRIP_PHOTOS)
    return count


# ── Step 1: prepare ───────────────────────────────────────────────────────────

def _window(first: str, last: str) -> Tuple[dt.date, dt.date, str, str]:
    """The local days album photos may have, and the UTC instants to ask
    Immich for so that every photo of those local days is answered."""
    lo = dt.date.fromisoformat(first) - _DAY_SLACK
    hi = dt.date.fromisoformat(last) + _DAY_SLACK
    after = dt.datetime.combine(lo, dt.time.min) - _UTC_AHEAD
    before = dt.datetime.combine(hi + dt.timedelta(days=1), dt.time.min) + _UTC_BEHIND
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return lo, hi, after.strftime(fmt), before.strftime(fmt)


def _local_day(asset) -> Optional[str]:
    """The asset's local capture day: ``localDateTime``'s date part as it
    stands (OD1), else the UTC capture day."""
    return normalise(asset.local_date_time) or normalise(asset.file_created_at)


def _prepare(scan_id: int) -> Optional[str]:
    if not _cas(scan_id, ("pending",), status="scanning", stage=STAGE_READING_ALBUM,
                progress_done=0, progress_total=0, heartbeat_at=time.time()):
        _log.info("Photo scan %s not started: no longer pending", scan_id)
        return None
    scan = _live_scan(scan_id, "scanning")
    if scan is None:
        return None

    with get_session() as sess:
        _check_trip_photo_count(_in_scope(sess, scan))
        first, last = project_day_bounds(sess, scan.project_id)

    rows: List[DBPhotoUpgradeAsset] = []
    seen = set()   # an album changed while paging can answer a photo twice
    if first is not None and last is not None:
        lo, hi, after, before = _window(first, last)
        client = _client_for(scan.user_info_id)
        for asset in client.iter_album_assets(scan.album_id, after, before):
            if asset.is_trashed or asset.is_archived:
                continue
            if asset.type not in (None, "IMAGE"):
                continue
            day = _local_day(asset)
            if day is None or not lo <= dt.date.fromisoformat(day) <= hi:
                continue
            if asset.id in seen:
                continue
            seen.add(asset.id)
            rows.append(DBPhotoUpgradeAsset(
                scan_id=scan_id, asset_id=asset.id, local_date=day,
                lat=asset.lat, lon=asset.lon, mime=asset.mime,
                file_bytes=asset.file_size,
                original_width=asset.width, original_height=asset.height))

    with get_session() as sess:
        if not _hold(sess, scan_id, stage=STAGE_FINGERPRINTING,
                     progress_done=0, progress_total=len(rows)):
            sess.rollback()
            return None
        sess.add_all(rows)
        sess.commit()
    _log.info("Photo scan %s: %d album photos to fingerprint", scan_id, len(rows))
    return "fingerprint" if rows else "match"


# ── Step 2: fingerprint ───────────────────────────────────────────────────────

def _fetch_fingerprint(client: ImmichClient, asset_id: str):
    """The asset's thumbnail fingerprint (None when it does not decode), or
    the ImmichError that stopped the fetch. Fetched and hashed in one go, so
    each thread holds one body at a time and none outlives this call."""
    try:
        raw = client.thumbnail_bytes(asset_id, _THUMB_SIZE)
    except ImmichError as exc:
        return exc
    return _album_fingerprint(raw)


def _album_fingerprint(raw: bytes) -> Optional[Fingerprint]:
    """*raw*'s fingerprint, or None when it is not a thumbnail-sized image.
    The size is read from the header before anything is decoded."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
    if width * height > _THUMB_MAX_PIXELS:
        return None
    return fingerprint(raw)


def _fingerprint(scan_id: int) -> Optional[str]:
    scan = _live_scan(scan_id, "scanning")
    if scan is None:
        return None
    with get_session() as sess:
        batch = sess.exec(
            select(DBPhotoUpgradeAsset.id, DBPhotoUpgradeAsset.asset_id)
            .where(DBPhotoUpgradeAsset.scan_id == scan_id,
                   DBPhotoUpgradeAsset.fingerprinted == False,  # noqa: E712
                   DBPhotoUpgradeAsset.fetch_failed == False)  # noqa: E712
            .order_by(DBPhotoUpgradeAsset.id)
            .limit(FINGERPRINT_BATCH)
        ).all()

    results: Dict[int, Optional[Fingerprint]] = {}
    if batch:
        client = _client_for(scan.user_info_id)
        with ThreadPoolExecutor(max_workers=FETCH_THREADS) as pool:
            fetched = list(pool.map(lambda row: _fetch_fingerprint(client, row[1]), batch))
        failures = [r for r in fetched if isinstance(r, ImmichError)]
        for exc in failures:
            # The key or the address is wrong for every photo, not this one.
            if isinstance(exc, ImmichAddressNotAllowed):
                raise exc
            if isinstance(exc, ImmichRejected) and exc.status in (401, 403):
                raise exc
        if failures and len(failures) == len(fetched) and all(
                isinstance(exc, ImmichUnreachable) for exc in failures):
            raise failures[0]
        for (row_id, _), got in zip(batch, fetched):
            results[row_id] = None if isinstance(got, ImmichError) else got

    with get_session() as sess:
        if not _hold(sess, scan_id):
            sess.rollback()
            return None
        for row_id, fp in results.items():
            if fp is None:
                values = {"fetch_failed": True}
            else:
                values = {"ahash": to_hex(fp.ahash), "dhash": to_hex(fp.dhash),
                          "width": fp.width, "height": fp.height, "fingerprinted": True}
            sess.execute(update(DBPhotoUpgradeAsset)
                         .where(DBPhotoUpgradeAsset.id == row_id).values(**values))
        remaining = sess.exec(
            select(func.count()).select_from(DBPhotoUpgradeAsset)
            .where(DBPhotoUpgradeAsset.scan_id == scan_id,
                   DBPhotoUpgradeAsset.fingerprinted == False,  # noqa: E712
                   DBPhotoUpgradeAsset.fetch_failed == False)  # noqa: E712
        ).one()
        total = sess.exec(
            select(func.count()).select_from(DBPhotoUpgradeAsset)
            .where(DBPhotoUpgradeAsset.scan_id == scan_id)).one()
        _hold(sess, scan_id, progress_done=total - remaining, progress_total=total)
        sess.commit()
    failed = sum(1 for fp in results.values() if fp is None)
    if failed:
        _log.info("Photo scan %s: %d album thumbnails could not be read", scan_id, failed)
    return "fingerprint" if remaining else "match"


# ── Step 3: match ─────────────────────────────────────────────────────────────

def _upright_size(path: Path) -> Optional[Tuple[int, int]]:
    """Width and height of the image at *path* as shown, from its header."""
    try:
        with Image.open(path) as img:
            width, height = img.size
            orientation = img.getexif().get(0x0112)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


class _Current:
    """A trip photo as stored, with where it sits."""

    def __init__(self, artifact: _Artifact, uuid: str, position: int):
        self.artifact = artifact
        self.uuid = uuid
        self.position = position
        self.key = photo_key(artifact.kind, artifact.id, uuid)
        self.bytes: Optional[int] = None
        self.size: Optional[Tuple[int, int]] = None


def _trip_photo(cur: _Current, folder: Path) -> Optional[TripPhoto]:
    """*cur* as the matcher sees it; None when its date cannot be read."""
    day = normalise(cur.artifact.date)
    if day is None:
        return None
    fp = None
    thumb = photo_file(folder, cur.uuid, "_thumb")
    if thumb is not None:
        try:
            fp = fingerprint(thumb.read_bytes())
        except OSError:
            fp = None
    full = photo_file(folder, cur.uuid)
    if full is not None:
        try:
            cur.bytes = full.stat().st_size
        except OSError:
            cur.bytes = None
        else:
            cur.size = _upright_size(full)
    state = cur.artifact.state
    return TripPhoto(
        key=cur.key, date=dt.date.fromisoformat(day),
        lat=cur.artifact.lat, lon=cur.artifact.lon, fp=fp,
        pixels=cur.size[0] * cur.size[1] if cur.size else None,
        source=state["sources"].get(cur.uuid),
        content_hash=state["hashes"].get(cur.uuid),
    )


def _album_photo(row: DBPhotoUpgradeAsset) -> AlbumPhoto:
    # ``width``/``height`` are the fingerprinted thumbnail's, upright: they
    # give the aspect ratio. The pixel count is the original's, from EXIF,
    # which turning does not change.
    known = row.original_width and row.original_height
    return AlbumPhoto(
        id=row.asset_id, local_date=dt.date.fromisoformat(row.local_date),
        lat=row.lat, lon=row.lon,
        fp=Fingerprint(from_hex(row.ahash), from_hex(row.dhash), row.width, row.height),
        pixels=row.original_width * row.original_height if known else None,
    )


def _original_size(row: DBPhotoUpgradeAsset) -> Tuple[Optional[int], Optional[int]]:
    """The original's width and height as shown: its EXIF pair, swapped when
    the upright thumbnail is portrait and the pair landscape, or the reverse.
    A square thumbnail or an unknown size keeps the EXIF order; no EXIF pair
    is (None, None)."""
    w, h = row.original_width, row.original_height
    if not (w and h):
        return None, None
    tw, th = row.width, row.height
    if tw and th and tw != th and w != h and (tw > th) != (w > h):
        return h, w
    return w, h


def photo_key(kind: str, artifact_id: int, photo_uuid: str) -> str:
    """The key a trip photo has in matching and in ``rejected_pairs_json``."""
    return f"{kind}:{artifact_id}:{photo_uuid}"


def _load_pairs(raw: Optional[str]) -> List[Tuple[str, str]]:
    """``rejected_pairs_json`` as (photo key, asset id) pairs; malformed
    entries are dropped one by one, an unreadable column reads as none."""
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [(p[0], p[1]) for p in value
            if isinstance(p, list) and len(p) == 2
            and isinstance(p[0], str) and isinstance(p[1], str)]


def _dump_pairs(pairs: Iterable[Tuple[str, str]]) -> str:
    return json.dumps([list(p) for p in sorted(set(pairs))])


def _earlier_rejections(sess, scan: DBPhotoUpgradeScan) -> Dict[str, List[str]]:
    """``{photo key: [asset id, ...]}`` the runner rejected on this trip in
    their earlier scans: those scans' rejected rows, newest scan first, then
    the rejections each of them carried from the scans before it (U9-R1-1)."""
    earlier = (DBPhotoUpgradeScan.project_id == scan.project_id,
               DBPhotoUpgradeScan.user_info_id == scan.user_info_id,
               DBPhotoUpgradeScan.id != scan.id)
    rows = sess.exec(
        select(DBPhotoUpgradeSuggestion.artifact_kind, DBPhotoUpgradeSuggestion.artifact_id,
               DBPhotoUpgradeSuggestion.photo_uuid, DBPhotoUpgradeSuggestion.asset_id)
        .join(DBPhotoUpgradeScan, DBPhotoUpgradeScan.id == DBPhotoUpgradeSuggestion.scan_id)
        .where(*earlier,
               DBPhotoUpgradeSuggestion.state == "rejected",
               DBPhotoUpgradeSuggestion.asset_id.is_not(None))
        .order_by(DBPhotoUpgradeScan.created_at.desc(), DBPhotoUpgradeSuggestion.id)
    ).all()
    pairs = [(photo_key(kind, artifact_id, uuid), asset_id)
             for kind, artifact_id, uuid, asset_id in rows]
    for raw in sess.exec(select(DBPhotoUpgradeScan.rejected_pairs_json).where(*earlier)
                         .order_by(DBPhotoUpgradeScan.created_at.desc())).all():
        pairs += sorted(_load_pairs(raw))
    found: Dict[str, List[str]] = {}
    for key, asset_id in pairs:
        assets = found.setdefault(key, [])
        if asset_id not in assets:
            assets.append(asset_id)
    return found


def record_rejection(scan_id: int, key: str, asset_id: str) -> bool:
    """Add (*key*, *asset_id*) to the scan's ``rejected_pairs_json``, for the
    route that rejects a suggestion. A compare-and-set on the column's old
    value, retried, so two rejections at once both land. False when the scan
    is gone."""
    for _ in range(5):
        with get_session() as sess:
            raw = sess.exec(select(DBPhotoUpgradeScan.rejected_pairs_json)
                            .where(DBPhotoUpgradeScan.id == scan_id)).first()
            if raw is None and sess.get(DBPhotoUpgradeScan, scan_id) is None:
                return False
            pairs = set(_load_pairs(raw))
            if (key, asset_id) in pairs:
                return True
            pairs.add((key, asset_id))
            result = sess.execute(
                update(DBPhotoUpgradeScan)
                .where(DBPhotoUpgradeScan.id == scan_id,
                       DBPhotoUpgradeScan.rejected_pairs_json == raw)
                .values(rejected_pairs_json=_dump_pairs(pairs)))
            sess.commit()
            if result.rowcount == 1:
                return True
    raise RuntimeError(f"rejection on photo scan {scan_id} lost five races")


def _match(scan_id: int) -> Optional[str]:
    scan = _live_scan(scan_id, "scanning")
    if scan is None:
        return None
    with get_session() as sess:
        artifacts = _in_scope(sess, scan)
        rejected = _earlier_rejections(sess, scan)
    total = _check_trip_photo_count(artifacts)
    if not _cas(scan_id, ("scanning",), stage=STAGE_MATCHING, progress_done=0,
                progress_total=total, heartbeat_at=time.time()):
        return None

    current: Dict[str, _Current] = {}
    photos: List[TripPhoto] = []
    for artifact in artifacts:
        folder = artifact.folder()
        for position, uuid in enumerate(artifact.photos):
            cur = _Current(artifact, uuid, position)
            photo = _trip_photo(cur, folder)
            if photo is None or cur.key in current:
                continue
            current[cur.key] = cur
            photos.append(photo)

    with get_session() as sess:
        assets = {row.asset_id: row for row in sess.exec(
            select(DBPhotoUpgradeAsset).where(
                DBPhotoUpgradeAsset.scan_id == scan_id,
                DBPhotoUpgradeAsset.fingerprinted == True)).all()}  # noqa: E712
    excluded = {(key, asset_id) for key, ids in rejected.items() for asset_id in ids}
    suggestions = match(photos, [_album_photo(r) for r in assets.values()], excluded)

    now = time.time()
    with get_session() as sess:
        # Every rejection so far goes onto this scan before the scans that
        # held them are deleted below (U9-R1-1).
        own = _load_pairs(sess.exec(select(DBPhotoUpgradeScan.rejected_pairs_json)
                                    .where(DBPhotoUpgradeScan.id == scan_id)).first())
        if not _hold(sess, scan_id, status="ready", stage=STAGE_DONE,
                     progress_done=total, completed_at=now,
                     rejected_pairs_json=_dump_pairs(list(excluded) + own)):
            sess.rollback()
            return None
        for s in suggestions:
            cur = current[s.key]
            state, asset_id, confidence = s.state, s.asset_id, s.confidence
            if state == NO_MATCH and s.key in rejected:
                # Keep the rejection on the row, so the scan after this one
                # still reads it once this one has replaced the earlier scan.
                state, asset_id, confidence = "rejected", rejected[s.key][0], None
            asset = assets.get(asset_id) if asset_id else None
            sess.add(DBPhotoUpgradeSuggestion(
                scan_id=scan_id, artifact_kind=cur.artifact.kind,
                artifact_id=cur.artifact.id, photo_uuid=cur.uuid,
                position=cur.position, date=normalise(cur.artifact.date),
                state=state, confidence=confidence,
                selected=state == PROPOSED and confidence == HIGH,
                asset_id=asset_id, score=s.score if state != "rejected" else None,
                alternatives_json=json.dumps(
                    [{"asset_id": a, "score": score} for a, score in s.alternatives]),
                asset_bytes=asset.file_bytes if asset is not None else None,
                asset_width=_original_size(asset)[0] if asset is not None else None,
                asset_height=_original_size(asset)[1] if asset is not None else None,
                asset_mime=asset.mime if asset is not None else None,
                current_bytes=cur.bytes,
                current_width=cur.size[0] if cur.size else None,
                current_height=cur.size[1] if cur.size else None,
            ))
        sess.commit()

    # Only now that this scan is ready (D8). The condition is in the DELETE,
    # so an apply started meanwhile keeps its scan.
    try:
        with get_session() as sess:
            gone = delete_photo_upgrade_scans(
                sess,
                DBPhotoUpgradeScan.project_id == scan.project_id,
                DBPhotoUpgradeScan.user_info_id == scan.user_info_id,
                DBPhotoUpgradeScan.id != scan_id,
                DBPhotoUpgradeScan.created_at <= scan.created_at,
                DBPhotoUpgradeScan.apply_status.not_in(_APPLY_IN_PROGRESS),
            )
            sess.commit()
    except Exception as exc:  # noqa: BLE001 — the scan stands; the sweep deletes them
        _log.error("Photo scan %s: earlier scans not deleted (%s)", scan_id,
                   type(exc).__qualname__)
        gone = 0
    _log.info("Photo scan %s ready: %d suggestions, %d earlier scans deleted",
              scan_id, len(suggestions), gone)
    return None


# ── The chain ─────────────────────────────────────────────────────────────────

_STEPS: Dict[str, Callable[[int], Optional[str]]] = {
    "prepare": _prepare,
    "fingerprint": _fingerprint,
    "match": _match,
}


def _run_step(name: str, scan_id: int) -> Optional[str]:
    """Run step *name*; the next step's name, or None when the chain ends.
    Any failure fails the scan. Never raises an ``Exception``."""
    try:
        return _STEPS[name](scan_id)
    except BaseException as exc:  # noqa: BLE001 — every failure ends the scan
        if not isinstance(exc, (_ScanFailed, ImmichError)):
            _log.error("Photo scan %s failed in %s: %s", scan_id, name, _describe(exc))
        else:
            _log.info("Photo scan %s stopped in %s: %s", scan_id, name,
                      type(exc).__qualname__)
        _fail(scan_id, _reason_for(exc))
        if not isinstance(exc, Exception):
            raise  # KeyboardInterrupt / SystemExit: let the process go
        return None


def _enqueue_next(name: Optional[str], scan_id: int) -> None:
    if name is None:
        return
    # Each job named at its call, never looked up: tests/test_jobs_never_call_strava.py
    # traces every queued callable to its module.
    options = {"max_retries": 0, "allow_inline": False}
    if name == "prepare":
        queued = enqueue(QUEUE_PHOTOS, run_scan_prepare, scan_id, **options)
    elif name == "fingerprint":
        queued = enqueue(QUEUE_PHOTOS, run_scan_fingerprint, scan_id, **options)
    else:
        queued = enqueue(QUEUE_PHOTOS, run_scan_match, scan_id, **options)
    if not queued:
        _fail(scan_id, REASON_UNAVAILABLE)


def run_scan_prepare(scan_id: int) -> None:
    """Job: step 1, then enqueue the next. Never raises."""
    _enqueue_next(_run_step("prepare", scan_id), scan_id)


def run_scan_fingerprint(scan_id: int) -> None:
    """Job: one batch of step 2, then enqueue the next. Never raises."""
    _enqueue_next(_run_step("fingerprint", scan_id), scan_id)


def run_scan_match(scan_id: int) -> None:
    """Job: step 3. Never raises."""
    _enqueue_next(_run_step("match", scan_id), scan_id)


def start_scan(scan_id: int) -> None:
    """Start the chain for a ``pending`` scan.

    With a broker, queues the first job on ``photos``, and fails the scan when
    it cannot. Without one, runs every step here, in this call: a caller in a
    request passes it to its background tasks.
    """
    if queue_available():
        _enqueue_next("prepare", scan_id)
        return
    step: Optional[str] = "prepare"
    while step is not None:
        step = _run_step(step, scan_id)


# ── Interruptions ─────────────────────────────────────────────────────────────

def mark_scan_interrupted(scan_id: int, reason: str) -> None:
    """Fail a ``pending`` or ``scanning`` scan whose job died where the chain
    could not see it (killed work-horse, dead worker, stale heartbeat). No-op
    on any other state."""
    if _fail(scan_id, reason):
        _log.info("Photo scan %s failed: interrupted", scan_id)


def sweep_scanning_scans() -> int:
    """Fail every ``scanning`` scan. Called by the ``photos`` worker as it
    starts, before it takes a job: it is the queue's only consumer
    (QUEUE_MAX_CONCURRENCY), so nothing is scanning right now. ``pending``
    scans are left alone: their job may still be waiting in Redis. Never
    raises."""
    try:
        with get_session() as sess:
            ids = list(sess.exec(select(DBPhotoUpgradeScan.id).where(
                DBPhotoUpgradeScan.status == "scanning")).all())
    except Exception:  # noqa: BLE001 — a broken sweep must not stop the worker
        _log.exception("Photo scan startup sweep could not read scans")
        return 0
    failed = 0
    for scan_id in ids:
        try:
            if _fail(scan_id, REASON_INTERRUPTED):
                failed += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the sweep
            _log.exception("Could not fail photo scan %s", scan_id)
    if failed:
        _log.info("Failed %d photo scan(s) left scanning by a previous worker", failed)
    return failed
