#!/usr/bin/env python
"""Probe a trip owner's Immich server before the album photo upgrade (#33).

The album upgrade plan (docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md) leaves five
questions open until they are answered on the owner's real server and album.
This script answers them and prints a report:

- OD1: the Immich version; whether ``POST /api/search/metadata`` takes
  ``albumIds`` with a date window and ``type: IMAGE`` (and keeps to the
  album), its page size and ``nextPage`` shape; whether ``localDateTime`` is
  present, with five sample pairs next to ``fileCreatedAt``.
- OD3: what ``thumbnail?size=thumbnail`` and ``size=fullsize`` serve for one
  JPEG, one HEIC and one RAW asset of the album: content type, size and
  pixels, against the original's EXIF pixels.
- OD4: whether 20 stored Polarsteps photos of the trip keep ``DateTimeOriginal``.
- OD5: which calls Immich refuses (401/403), with the key permission each needs.

For OD2 it writes an HTML contact sheet (``--out``): each trip photo of the
first ``--limit-days`` days that have photos, next to its 3 nearest album
photos taken within a day of it, with their aHash and dHash distances, aspect
ratios and day offset. The thresholds are read off that sheet. Images are
embedded in the file, so it holds the owner's photos: keep it private.

The Immich server and key are the trip owner's, as stored by the app
(``immichtoken``, encrypted): run it with the server's
``CREDENTIALS_ENCRYPTION_KEY``. The key and image bytes are never printed.

READ-ONLY: the database is opened with ``PRAGMA query_only``, photo files are
only read, and ``--out`` may not point into the data directory. Without
``--album-id`` it lists the owner's albums instead.

Usage:
    python scripts/immich_upgrade_probe.py --db traxjourney.db --data-dir data \\
        --project-id 12
    python scripts/immich_upgrade_probe.py --db traxjourney.db --data-dir data \\
        --project-id 12 --album-id <uuid> --out probe.html --limit-days 3
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlmodel import Session, create_engine, or_, select  # noqa: E402

import api.immich as immich  # noqa: E402
from models.project_db import DBJournalEntry, DBMemory, DBProject  # noqa: E402
from models.user import ImmichToken  # noqa: E402
from src.utils.photo_paths import is_photo_name, photo_file, photo_folder  # noqa: E402

#: Stored Polarsteps photos checked for EXIF capture time (OD4).
EXIF_SAMPLE = 20
#: Album photos shown per trip photo on the contact sheet.
CANDIDATES_SHOWN = 3
#: Search pages followed at most: 10,000 photos at Immich's default 250 (D12).
MAX_PAGES = 40
#: Bytes kept of a fullsize image: enough for its header, read for the pixels.
_HEADER_BYTES = 2 * 1024 * 1024
#: Largest thumbnail kept in memory to hash.
_MAX_THUMB_BYTES = 10 * 1024 * 1024
#: Longest side of the images embedded in the contact sheet.
_SHEET_PX = 200

_RAW_EXTENSIONS = {"dng", "cr2", "cr3", "nef", "nrw", "arw", "srf", "sr2", "raf", "orf",
                   "rw2", "pef", "srw", "x3f", "3fr", "iiq", "erf", "kdc", "mrw", "raw"}

# EXIF: the Exif sub-IFD, and its capture time and offset tags.
_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_OFFSET_TIME_ORIGINAL = 36881


# ── Hashing: a local copy of what src/photo_upgrade/matching.py will own (U5) ──

def ahash(img: Image.Image) -> int:
    """64-bit average hash of an upright image."""
    px = list(img.convert("L").resize((8, 8), Image.LANCZOS).tobytes())
    mean = sum(px) / 64
    return sum(1 << i for i, p in enumerate(px) if p > mean)


def dhash(img: Image.Image) -> int:
    """64-bit difference hash: each pixel against its right neighbour."""
    px = list(img.convert("L").resize((9, 8), Image.LANCZOS).tobytes())
    bits = [px[r * 9 + c] > px[r * 9 + c + 1] for r in range(8) for c in range(8)]
    return sum(1 << i for i, b in enumerate(bits) if b)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class Fingerprint:
    ahash: int
    dhash: int
    aspect: float  # upright width / height
    preview: str   # data URI for the sheet


def fingerprint(raw: bytes) -> Fingerprint:
    """Hash an image upright. Raises OSError/ValueError when it does not decode."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    img = img.convert("RGB")
    fp_a, fp_d = ahash(img), dhash(img)
    aspect = img.width / img.height
    img.thumbnail((_SHEET_PX, _SHEET_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    preview = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return Fingerprint(fp_a, fp_d, aspect, preview)


# ── Immich calls ────────────────────────────────────────────────────────────

@dataclass
class Call:
    name: str
    permission: str
    status: Optional[int]
    error: Optional[str] = None


@dataclass
class Report:
    lines: List[str] = field(default_factory=list)
    calls: List[Call] = field(default_factory=list)

    def say(self, line: str = "") -> None:
        self.lines.append(line)


class Immich:
    """The owner's server, through api.immich's guarded fetch (read at call
    time, so a test can patch ``api.immich._http``). Every call is recorded."""

    def __init__(self, server_url: str, api_key: str, report: Report):
        self._base = server_url.rstrip("/")
        self._headers = immich._headers(api_key)
        self._report = report

    def _record(self, name, permission, status, error=None) -> None:
        self._report.calls.append(Call(name, permission, status, error))

    def json(self, method: str, path: str, permission: str, body=None):
        """(status, decoded JSON or None). Status None when the call failed."""
        url = f"{self._base}{path}"
        try:
            if method == "POST":
                resp = immich._http.post(url, headers=self._headers, json=body,
                                         timeout=immich._SEARCH_TIMEOUT)
            else:
                resp = immich._http.get(url, headers=self._headers,
                                        timeout=immich._SEARCH_TIMEOUT)
        except (immich.ImmichAddressNotAllowed, requests.RequestException) as exc:
            self._record(f"{method} {path}", permission, None, type(exc).__name__)
            return None, None
        self._record(f"{method} {path}", permission, resp.status_code)
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None

    def bytes(self, path: str, permission: str, keep: int):
        """(status, content type, first *keep* bytes, total byte count)."""
        url = f"{self._base}{path}"
        try:
            resp = immich._http.get(url, headers=self._headers,
                                    timeout=immich._DOWNLOAD_TIMEOUT, stream=True)
        except (immich.ImmichAddressNotAllowed, requests.RequestException) as exc:
            self._record(f"GET {path}", permission, None, type(exc).__name__)
            return None, None, b"", 0
        self._record(f"GET {path}", permission, resp.status_code)
        try:
            if resp.status_code != 200:
                return resp.status_code, None, b"", 0
            head, total = bytearray(), 0
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if len(head) < keep:
                        head += chunk[: keep - len(head)]
            except requests.RequestException:
                return resp.status_code, None, b"", 0
            return resp.status_code, resp.headers.get("content-type"), bytes(head), total
        finally:
            resp.close()

    def status_only(self, path: str, permission: str) -> Optional[int]:
        """Open a download and close it unread: only its status is wanted."""
        url = f"{self._base}{path}"
        try:
            resp = immich._http.get(url, headers=self._headers,
                                    timeout=immich._DOWNLOAD_TIMEOUT, stream=True)
        except (immich.ImmichAddressNotAllowed, requests.RequestException) as exc:
            self._record(f"GET {path}", permission, None, type(exc).__name__)
            return None
        self._record(f"GET {path}", permission, resp.status_code)
        resp.close()
        return resp.status_code


# ── Trip photos ─────────────────────────────────────────────────────────────

@dataclass
class TripPhoto:
    kind: str       # "memory" | "journal"
    row_id: int
    day: str        # "YYYY-MM-DD"
    name: str
    folder: Path


def _names(photos_json) -> List[str]:
    try:
        photos = json.loads(photos_json or "[]")
    except (TypeError, ValueError):
        return []
    return [p for p in photos if is_photo_name(p)] if isinstance(photos, list) else []


def trip_photos(sess: Session, project: DBProject, data_dir: Path) -> List[TripPhoto]:
    """Every stored photo of the trip's memories and of the owner's journal
    entries, in date order."""
    owner = project.user_info_id
    found: List[TripPhoto] = []
    for row in sess.exec(select(DBMemory).where(DBMemory.project_id == project.id)
                         .order_by(DBMemory.date, DBMemory.id)).all():
        folder = photo_folder(data_dir, owner, "memories", row.id)
        found += [TripPhoto("memory", row.id, row.date, n, folder) for n in _names(row.photos_json)]
    for row in sess.exec(select(DBJournalEntry).where(
            DBJournalEntry.project_id == project.id,
            or_(DBJournalEntry.user_info_id == owner, DBJournalEntry.user_info_id.is_(None)))
            .order_by(DBJournalEntry.date, DBJournalEntry.id)).all():
        folder = photo_folder(data_dir, owner, "journal", row.id)
        found += [TripPhoto("journal", row.id, row.date, n, folder) for n in _names(row.photos_json)]
    found.sort(key=lambda p: (p.day, p.kind, p.row_id))
    return found


def probe_polarsteps_exif(sess: Session, project: DBProject, data_dir: Path, report: Report) -> None:
    """OD4: do stored Polarsteps photos keep their EXIF capture time?"""
    rows = sess.exec(select(DBMemory).where(DBMemory.project_id == project.id,
                                            DBMemory.polarsteps_step_id.is_not(None))
                     .order_by(DBMemory.date, DBMemory.id)).all()
    checked, with_dto, with_offset, samples = 0, 0, 0, []
    for row in rows:
        folder = photo_folder(data_dir, project.user_info_id, "memories", row.id)
        for name in _names(row.photos_json):
            if checked >= EXIF_SAMPLE:
                break
            path = photo_file(folder, name)
            if path is None or not path.is_file():
                continue
            checked += 1
            try:
                with Image.open(path) as img:
                    ifd = img.getexif().get_ifd(_EXIF_IFD)
            except (OSError, ValueError):
                continue
            dto, offset = ifd.get(_DATETIME_ORIGINAL), ifd.get(_OFFSET_TIME_ORIGINAL)
            if dto:
                with_dto += 1
                if len(samples) < 5:
                    samples.append(f"memory {row.id} ({row.date}): {dto} {offset or ''}".rstrip())
            if offset:
                with_offset += 1
    report.say("== OD4: EXIF on stored Polarsteps photos ==")
    if not checked:
        report.say("no stored Polarsteps photo found in this trip")
    else:
        report.say(f"{with_dto}/{checked} have DateTimeOriginal; {with_offset}/{checked} "
                   "have OffsetTimeOriginal")
        report.lines += [f"  {s}" for s in samples]
    report.say()


# ── Album probes ────────────────────────────────────────────────────────────

def _items(answer) -> Tuple[List[dict], object]:
    """(items, nextPage) of a search/metadata answer."""
    assets = answer.get("assets") if isinstance(answer, dict) else None
    if not isinstance(assets, dict):
        return [], None
    items = assets.get("items")
    return ([i for i in items if isinstance(i, dict)] if isinstance(items, list) else []), \
        assets.get("nextPage")


def local_day(asset: dict) -> Tuple[Optional[str], bool]:
    """(capture day, from localDateTime?). Falls back to fileCreatedAt."""
    for key, local in (("localDateTime", True), ("fileCreatedAt", False)):
        value = asset.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10], local
    return None, False


def _is_image(asset: dict) -> bool:
    return asset.get("type") == "IMAGE" and not asset.get("isTrashed")


def asset_kind(asset: dict) -> Optional[str]:
    """"jpeg", "heic" or "raw", from the file name and MIME type."""
    name = asset.get("originalFileName") or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = (asset.get("originalMimeType") or "").lower()
    if ext in ("heic", "heif") or mime in ("image/heic", "image/heif"):
        return "heic"
    if ext in _RAW_EXTENSIONS:
        return "raw"
    if ext in ("jpg", "jpeg") or mime == "image/jpeg":
        return "jpeg"
    return None


def _describe_image(head: bytes) -> str:
    try:
        with Image.open(io.BytesIO(head)) as img:
            return f"{img.format} {img.width}x{img.height}"
    except (OSError, ValueError):
        return "not decodable by Pillow"


def search_album(client: Immich, album_id: str, after: str, before: str,
                 album_ids: Optional[set], report: Report) -> Optional[List[dict]]:
    """OD1: search/metadata with albumIds, a date window and type IMAGE.
    Returns every item of the window, or None when it cannot be used."""
    body = {"albumIds": [album_id], "takenAfter": after, "takenBefore": before,
            "type": "IMAGE", "withExif": True}
    report.say("== OD1: search/metadata with albumIds + date window + type IMAGE ==")
    report.say(f"window (UTC): {after} .. {before}")
    status, answer = client.json("POST", "/api/search/metadata", "asset.read", body)
    if status != 200 or answer is None:
        report.say(f"refused or unreadable (HTTP {status}): list the album instead")
        report.say()
        return None
    items, next_page = _items(answer)
    total = (answer.get("assets") or {}).get("total") if isinstance(answer, dict) else None
    report.say(f"page 1 (no size sent): {len(items)} items, total={total!r}, "
               f"nextPage={next_page!r} ({type(next_page).__name__})")
    pages = 1
    while next_page is not None and pages < MAX_PAGES:
        status, answer = client.json("POST", "/api/search/metadata", "asset.read",
                                     {**body, "page": next_page})
        if status != 200 or answer is None:
            report.say(f"page {pages + 1} failed (HTTP {status})")
            break
        more, next_page = _items(answer)
        pages += 1
        if pages == 2:
            report.say(f"page 2: {len(more)} items, nextPage={next_page!r}")
        items += more
    if next_page is not None:
        report.say(f"stopped after {pages} pages: more remain")
    report.say(f"{len(items)} items over {pages} page(s)")
    types = sorted({str(i.get("type")) for i in items})
    report.say(f"types returned: {', '.join(types) or '-'}")
    if album_ids is None:
        report.say("album membership unverified: the album answer listed no assets")
    else:
        outside = [i for i in items if i.get("id") not in album_ids]
        report.say(f"items outside the album: {len(outside)}"
                   + (" -> albumIds is NOT honoured" if outside else " (albumIds honoured)"))
        if outside:
            report.say()
            return None
    report.say()
    return items


def report_local_time(assets: List[dict], report: Report) -> None:
    report.say("== OD1: localDateTime ==")
    present = [a for a in assets if isinstance(a.get("localDateTime"), str)]
    report.say(f"present on {len(present)}/{len(assets)} assets")
    for a in present[:5]:
        report.say(f"  fileCreatedAt={a.get('fileCreatedAt')}  localDateTime={a.get('localDateTime')}")
    report.say()


def probe_formats(client: Immich, assets: List[dict], report: Report) -> None:
    """OD3 (and OD5's download permission): what the thumbnail sizes serve."""
    report.say("== OD3: thumbnail formats ==")
    picked: Dict[str, dict] = {}
    for a in assets:
        kind = asset_kind(a)
        if kind and kind not in picked:
            picked[kind] = a
    for kind in ("jpeg", "heic", "raw"):
        a = picked.get(kind)
        if a is None:
            report.say(f"{kind}: none in the album")
            continue
        aid = quote(str(a.get("id")), safe="")
        exif = a.get("exifInfo") if isinstance(a.get("exifInfo"), dict) else {}
        report.say(f"{kind}: asset {a.get('id')} ({a.get('originalMimeType')}), original "
                   f"{exif.get('exifImageWidth')}x{exif.get('exifImageHeight')} per EXIF")
        for size, keep in (("thumbnail", _MAX_THUMB_BYTES), ("fullsize", _HEADER_BYTES)):
            status, ctype, head, total = client.bytes(
                f"/api/assets/{aid}/thumbnail?size={size}", "asset.view", keep)
            if status != 200:
                report.say(f"  size={size}: HTTP {status}")
            else:
                report.say(f"  size={size}: {ctype}, {total} bytes, {_describe_image(head)}")
    if picked:
        first = next(iter(picked.values()))
        status = client.status_only(f"/api/assets/{quote(str(first.get('id')), safe='')}/original",
                                    "asset.download")
        report.say(f"original download: HTTP {status}")
    report.say()


# ── Calibration ─────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    asset_id: str
    fp: Fingerprint
    a_dist: int
    d_dist: int
    day_offset: int

    @property
    def score(self) -> int:
        return self.a_dist + self.d_dist


@dataclass
class SheetRow:
    photo: TripPhoto
    fp: Fingerprint
    candidates: List[Candidate]   # nearest first, at most CANDIDATES_SHOWN
    window_size: int              # album photos within a day


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def calibrate(client: Immich, photos: List[TripPhoto], assets: List[dict],
              report: Report) -> List[SheetRow]:
    """For each trip photo, its nearest album photos within ±1 local day."""
    by_day: Dict[str, List[dict]] = {}
    for a in assets:
        day, _ = local_day(a)
        if day and isinstance(a.get("id"), str):
            by_day.setdefault(day, []).append(a)
    cache: Dict[str, Optional[Fingerprint]] = {}

    def asset_fp(asset_id: str) -> Optional[Fingerprint]:
        if asset_id not in cache:
            status, _, raw, total = client.bytes(
                f"/api/assets/{quote(asset_id, safe='')}/thumbnail?size=thumbnail",
                "asset.view", _MAX_THUMB_BYTES)
            fp = None
            if status == 200 and total <= _MAX_THUMB_BYTES:
                try:
                    fp = fingerprint(raw)
                except (OSError, ValueError):
                    fp = None
            cache[asset_id] = fp
        return cache[asset_id]

    rows: List[SheetRow] = []
    unreadable_trip = 0
    for photo in photos:
        thumb = photo_file(photo.folder, photo.name, "_thumb")
        try:
            if thumb is None:
                raise OSError("not a photo file")
            fp = fingerprint(thumb.read_bytes())
        except (OSError, ValueError):
            unreadable_trip += 1
            continue
        found: List[Candidate] = []
        for offset in (-1, 0, 1):
            for a in by_day.get(_shift(photo.day, offset), []):
                afp = asset_fp(a["id"])
                if afp is not None:
                    found.append(Candidate(a["id"], afp, hamming(fp.ahash, afp.ahash),
                                           hamming(fp.dhash, afp.dhash), offset))
        found.sort(key=lambda c: (c.score, c.asset_id))
        rows.append(SheetRow(photo, fp, found[:CANDIDATES_SHOWN], len(found)))

    report.say("== OD2: calibration ==")
    report.say(f"{len(rows)} trip photo(s) hashed, {unreadable_trip} unreadable; "
               f"{sum(1 for f in cache.values() if f)} album thumbnail(s) hashed, "
               f"{sum(1 for f in cache.values() if f is None)} unreadable")
    best = sorted(r.candidates[0].score for r in rows if r.candidates)
    margins = sorted(r.candidates[1].score - r.candidates[0].score
                     for r in rows if len(r.candidates) > 1)
    report.say(f"best score (0-128) per photo: {_spread(best)}")
    report.say(f"margin to the runner-up:       {_spread(margins)}")
    report.say()
    return rows


def _spread(values: List[int]) -> str:
    if not values:
        return "-"
    pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]  # noqa: E731
    return f"min {values[0]}, p10 {pick(.1)}, median {pick(.5)}, p90 {pick(.9)}, max {values[-1]}"


def render_sheet(rows: List[SheetRow], title: str) -> str:
    """The contact sheet: one row per trip photo, its nearest candidates."""
    esc = html.escape
    out = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{esc(title)}</title><style>",
        "body{font-family:sans-serif;font-size:12px}td{vertical-align:top;padding:4px;"
        "border-bottom:1px solid #ccc}img{display:block;max-width:200px;max-height:200px}"
        ".photo{background:#eef}",
        "</style></head><body>",
        f"<h1>{esc(title)}</h1>",
        "<p>Score = aHash + dHash Hamming distance (0-128, lower is closer). "
        "Aspect = upright width/height. Day = album photo's local day minus the trip day.</p>",
        "<table>",
    ]
    for r in rows:
        p = r.photo
        out.append(f"<tr data-photo='{esc(p.name)}'><td class='photo'>"
                   f"<img src='{r.fp.preview}' alt=''>{esc(p.kind)} {p.row_id}<br>"
                   f"{esc(p.day)}<br>aspect {r.fp.aspect:.3f}<br>"
                   f"{r.window_size} album photo(s) within a day</td>")
        for c in r.candidates:
            out.append(f"<td class='candidate'><img src='{c.fp.preview}' alt=''>"
                       f"{esc(c.asset_id)}<br>score {c.score} (a {c.a_dist}, d {c.d_dist})<br>"
                       f"aspect {c.fp.aspect:.3f}<br>day {c.day_offset:+d}</td>")
        out.append("</tr>")
    out.append("</table></body></html>")
    return "\n".join(out)


# ── Main ────────────────────────────────────────────────────────────────────

def read_only_engine(db: Path):
    """The database, refusing every write on every connection."""
    engine = create_engine(f"sqlite:///{db}")

    @event.listens_for(engine, "connect")
    def _query_only(dbapi_conn, _record):  # pragma: no cover - thin
        dbapi_conn.execute("PRAGMA query_only=ON")

    return engine


def report_refusals(report: Report) -> None:
    report.say("== OD5: refused calls ==")
    refused = [c for c in report.calls if c.status in (401, 403)]
    failed = [c for c in report.calls if c.status is None]
    for c in refused:
        report.say(f"HTTP {c.status}: {c.name} (key permission: {c.permission})")
    for c in failed:
        report.say(f"no answer: {c.name} ({c.error})")
    if not refused:
        report.say("no call answered 401 or 403")
    needed = sorted({c.permission for c in report.calls} - {"none"})
    report.say(f"permissions exercised: {', '.join(needed)}")


def run(sess: Session, args, data_dir: Path) -> Tuple[int, Report]:
    report = Report()
    project = sess.get(DBProject, args.project_id)
    if project is None:
        report.say(f"ERROR: project {args.project_id} not found")
        return 2, report
    try:
        tok = sess.exec(select(ImmichToken).where(
            ImmichToken.user_info_id == project.user_info_id)).first()
    except RuntimeError as exc:  # no or a malformed CREDENTIALS_ENCRYPTION_KEY
        report.say(f"ERROR: {exc}")
        return 2, report
    if tok is None or not tok.api_key or not tok.server_url:
        report.say(f"ERROR: the owner of project {project.id} has no readable Immich "
                   "connection (not connected, or CREDENTIALS_ENCRYPTION_KEY differs)")
        return 2, report
    client = Immich(tok.server_url, tok.api_key, report)
    report.say(f"=== Immich upgrade probe: project {project.id} ({project.name}) ===")
    report.say()

    report.say("== OD1: Immich version ==")
    status, about = client.json("GET", "/api/server/about", "server.about")
    if isinstance(about, dict) and about.get("version"):
        report.say(f"version {about['version']}")
    else:
        status, ver = client.json("GET", "/api/server/version", "none")
        if isinstance(ver, dict):
            report.say("version v{major}.{minor}.{patch} (server/about refused)".format(
                **{k: ver.get(k) for k in ("major", "minor", "patch")}))
        else:
            report.say(f"unknown (HTTP {status})")
    report.say()

    probe_polarsteps_exif(sess, project, data_dir, report)

    if not args.album_id:
        report.say("== Albums (pass one with --album-id) ==")
        status, albums = client.json("GET", "/api/albums", "album.read")
        for a in sorted((a for a in albums or [] if isinstance(a, dict)),
                        key=lambda a: str(a.get("albumName"))):
            report.say(f"{a.get('id')}  {a.get('albumName')}  ({a.get('assetCount')} assets)")
        if albums is None:
            report.say(f"could not list albums (HTTP {status})")
        report.say()
        report_refusals(report)
        return 0, report

    status, album = client.json("GET", f"/api/albums/{quote(args.album_id, safe='')}", "album.read")
    album_assets: Optional[List[dict]] = None
    report.say("== Album ==")
    if isinstance(album, dict):
        listed = album.get("assets")
        if isinstance(listed, list) and listed:
            album_assets = [a for a in listed if isinstance(a, dict)]
        report.say(f"{album.get('albumName')}: {album.get('assetCount')} assets, "
                   f"{len(album_assets or [])} listed in the album answer")
    else:
        report.say(f"could not read the album (HTTP {status})")
    report.say()

    photos = trip_photos(sess, project, data_dir)
    days = sorted({p.day for p in photos})[: args.limit_days]
    photos = [p for p in photos if p.day in days]
    if not days:
        report.say("the trip has no stored photo: nothing to calibrate")
        report_refusals(report)
        return 0, report
    # ±1 local day, plus the widest UTC offsets (-12h/+14h): two days each side.
    after = f"{_shift(days[0], -2)}T00:00:00.000Z"
    before = f"{_shift(days[-1], 3)}T00:00:00.000Z"
    album_ids = {a.get("id") for a in album_assets} if album_assets is not None else None
    searched = search_album(client, args.album_id, after, before, album_ids, report)
    if searched is not None:
        pool, source = searched, "search/metadata"
    else:
        pool, source = album_assets or [], "the album's asset list"
    pool = [a for a in pool if _is_image(a)]
    report_local_time(pool, report)
    probe_formats(client, album_assets if album_assets else pool, report)

    report.say(f"calibrating days {days[0]} .. {days[-1]} ({len(days)} day(s), "
               f"{len(photos)} photo(s)) against {source}")
    rows = calibrate(client, photos, pool, report)
    out = Path(args.out)
    out.write_text(render_sheet(rows, f"Immich upgrade probe: project {project.id}"),
                   encoding="utf-8")
    report.say(f"contact sheet written: {out}")
    report.say()
    report_refusals(report)
    return 0, report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to the SQLite database")
    ap.add_argument("--data-dir", default="data", help="the data/ dir holding user photo files")
    ap.add_argument("--project-id", type=int, required=True, help="the trip to probe")
    ap.add_argument("--album-id", help="the Immich album holding the trip's originals; "
                                       "omit to list albums")
    ap.add_argument("--out", default="immich_upgrade_probe.html",
                    help="where to write the HTML contact sheet")
    ap.add_argument("--limit-days", type=int, default=3,
                    help="calibrate the first N trip days that have photos (default 3)")
    args = ap.parse_args(argv)

    db, data_dir = Path(args.db), Path(args.data_dir)
    if not db.is_file():
        print(f"ERROR: database not found: {db}", file=sys.stderr)
        return 2
    if args.limit_days < 1:
        print("ERROR: --limit-days must be at least 1", file=sys.stderr)
        return 2
    out = Path(args.out).resolve()
    if out.is_relative_to(data_dir.resolve()) or out == db.resolve():
        print("ERROR: --out may not point into the data directory or at the database",
              file=sys.stderr)
        return 2

    engine = read_only_engine(db)
    try:
        with Session(engine) as sess:
            code, report = run(sess, args, data_dir)
    finally:
        engine.dispose()
    print("\n".join(report.lines), file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
