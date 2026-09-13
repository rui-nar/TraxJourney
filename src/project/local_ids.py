"""Identity for activities the app creates itself (issue #260, unit 2).

Two things live here, and they are separate on purpose:

**Local ids.** ``activity.id`` is a global primary key whose positive range
belongs to Strava — a row's id IS its Strava id, which is what makes an upsert
from a sync idempotent. Anything the app creates itself therefore takes a
NEGATIVE id: a split tail, or a GPX import. Both used to allocate their own way
— a split took ``min(0, global_min) - 1`` and a GPX import a random 62-bit
value — which meant two schemes sharing one keyspace with different collision
properties, and the dense one was unsafe across instances: a ``.traxj``
exported from one deployment and imported into another carries its ids with it,
and ids -1, -2, -3 exist in every deployment.

**Source ids.** A content fingerprint of what was imported, so the same file
imported twice is recognised as the same activity rather than silently becoming
two overlapping ones. Distinct from the row id because it answers a different
question: the id says *which row*, the source id says *which real-world thing*.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Iterable, Optional, Tuple

from sqlmodel import Session

from models.project_db import DBActivity

#: Bits of randomness in a local id. The ceiling is not the database column —
#: that would take 62 — it is JavaScript. The web client is compiled with
#: dart2js, where Dart's ``int`` IS an IEEE-754 double, so an id beyond
#: 2^53 is silently rounded the moment ``jsonDecode`` sees it. The client then
#: sends the rounded value back on every edit, split and delete, and the server
#: answers 404 for an activity that plainly exists on screen.
#:
#: 53 bits keeps every id inside ``Number.MAX_SAFE_INTEGER`` (2^53 - 1), and
#: still leaves the collision probability negligible: n ids collide with odds
#: about n^2 / 2^54, which is one in eighteen million at a million local
#: activities — against the decrementing scheme it replaces, which handed out
#: -1, -2, -3 in every deployment and so collided with certainty once a
#: ``.traxj`` crossed between two.
#:
#: Android and iOS carry real 64-bit ints and would not have noticed. The web
#: client is where this bites, and GPX import has been drawing 62-bit ids since
#: it shipped — so this is a fix, not only a precaution.
_LOCAL_ID_BITS = 53

#: How many times to re-draw before giving up. Each attempt is an independent
#: 1-in-2^53 shot at an existing row, so exhausting five means something is
#: wrong with the session, not with luck.
_ALLOCATION_ATTEMPTS = 5

#: Coordinates are rounded to this many decimal places before hashing — about
#: 10 cm at the equator. Two imports of the same file must hash identically, and
#: a float that survived a JSON round trip may differ in its last bits; 7 places
#: is far below GPS precision and far above that noise.
_FINGERPRINT_PRECISION = 7


class LocalIdExhausted(RuntimeError):
    """Raised when no free local id could be drawn — see _ALLOCATION_ATTEMPTS."""


def allocate_local_activity_id(sess: Session) -> int:
    """Return an unused negative activity id.

    Used by every path that creates an activity the app owns, so all of them
    share one keyspace with one set of collision properties. Checks the
    ``activity`` table rather than this project's timeline: a tail whose item
    was removed leaves the row behind, and reusing its id would collide on
    INSERT.
    """
    for _ in range(_ALLOCATION_ATTEMPTS):
        candidate = -secrets.randbits(_LOCAL_ID_BITS)
        if sess.get(DBActivity, candidate) is None:
            return candidate
    raise LocalIdExhausted(
        f"no free local activity id after {_ALLOCATION_ATTEMPTS} attempts")


def track_fingerprint(points: Iterable[Tuple[float, float]],
                      started_at: Optional[str] = None) -> str:
    """Fingerprint a track's geometry, for recognising a re-import.

    Hashes the rounded coordinates in order, plus the activity's start time when
    one is known. Geometry alone would call two laps of the same loop the same
    activity; the start time separates them while still matching the same file
    imported twice.

    That start time is whatever the importer was given, which today is typed by
    the user — so the same file imported twice with a mistyped time is NOT
    recognised, and lands as a second activity. Deliberate: the alternative,
    ignoring time, merges a planned route genuinely ridden on two days. Unit 3
    takes the time from the file where it has one, which narrows the gap to
    files that carry none.

    Elevation is deliberately excluded. It is the part of a track most likely to
    be rewritten after import — by an edit, or by the dropout repair in
    ``c4a9e1f70b38`` — and a fingerprint that changes when the app corrects its
    own data would stop recognising the file it came from.
    """
    digest = hashlib.sha256()
    if started_at:
        digest.update(started_at.encode("utf-8"))
        digest.update(b"\x00")
    for lat, lng in points:
        digest.update(f"{round(lat, _FINGERPRINT_PRECISION)},"
                      f"{round(lng, _FINGERPRINT_PRECISION)};".encode("ascii"))
    return digest.hexdigest()
