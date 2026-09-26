"""repair NaN and Infinity stored as elevations (issue #462)

gpxpy reads ``<ele>NaN</ele>`` and ``<ele>inf</ele>`` as floats, and until #462
the GPX upload never checked them (nor did the track editor, nor the Strava
stream code): an activity could store NaN or Infinity in its elevation
profile (JSON text holding the ``NaN``/``Infinity`` tokens), and Infinity in
its gain and high/low (SQLite stores a NaN REAL as NULL, so only Infinity
survives in those columns). The trip's JSON then held tokens the client's
parser refuses, so the trip would not open, and its export was a file the
import now refuses.

The writers now treat a non-finite elevation as a missing one. This repairs
what they already stored, to exactly what they would store now, through the
same code (imported from the app, as c4a9e1f70b38 and b7f1a3c9d204 do):

* a profile, and the edit-undo snapshot of one (which a reset restores
  verbatim), is rebuilt by ``clean_elevation_profile``: a non-finite reading
  is filled by distance like a missing ``<ele>``, a sample with no usable
  distance is dropped, and a profile left with no reading becomes NULL. The
  low-res copy the chart loads first is rebuilt from it;
* on a row whose figures the app computed itself (a GPX upload or an edited
  track), gain, high and low are recomputed from the repaired profile, as
  c4a9e1f70b38 recomputes them: they were measured from the same broken
  readings. A Strava row keeps Strava's own figures;
* a figure still not finite after that is recomputed from the profile if
  there is one, and otherwise cleared: gain to 0.0 (its NOT NULL column's
  default, what an activity without elevation stores), high and low to NULL,
  the snapshot's gain to NULL (a reset then recomputes it);
* every trip holding a repaired row has its cached totals dropped and its
  lock_version advanced, so a client's on-device copy is refetched (#173).

A profile that is a client-side E2EE envelope is left alone: the server holds
no key for it (issue #366).

Idempotent: a repaired row holds no non-finite value, so a second run finds
nothing. The downgrade does nothing: the values replaced are not worth
restoring, and the old code would only write new ones.

Revision ID: 6c1f0e9a2b47
Revises: 5e2b7c1d9a40
Create Date: 2026-09-26

"""
import json
import logging
import math
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '6c1f0e9a2b47'
down_revision: Union[str, Sequence[str], None] = '5e2b7c1d9a40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

_PROFILES = ("elevation_profile_json", "elevation_profile_low_res_json",
             "original_elevation_profile_json")
_FIGURES = ("total_elevation_gain", "elev_high", "elev_low",
            "original_total_elevation_gain")

#: Finds the rows to look at in SQL, so a database of clean activities is not
#: read into Python. The JSON tokens json.dumps writes for a non-finite float,
#: and a REAL past the largest finite double (on PostgreSQL NaN also sorts
#: above it). Ciphertext can spell a token too: those rows are skipped below.
_CANDIDATES = (
    "SELECT id, source, is_edited, " + ", ".join(_PROFILES + _FIGURES)
    + " FROM activity WHERE "
    + " OR ".join(f"{c} LIKE '%NaN%' OR {c} LIKE '%Infinity%'" for c in _PROFILES)
    + " OR "
    + " OR ".join(f"{c} > 1.7976931348623157e308 OR {c} < -1.7976931348623157e308"
                  for c in _FIGURES)
)


def _finite(value) -> bool:
    return value is None or math.isfinite(value)


def _parsed(ep_json):
    """``(distances, elevations)`` of a stored plaintext profile, or None."""
    try:
        ep = json.loads(ep_json)
        return (ep.get("distances_km") or []), (ep.get("elevations_m") or [])
    except (ValueError, TypeError, AttributeError):
        return None


def _broken(ep) -> bool:
    return ep is not None and not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in ep[0] + ep[1])


def upgrade() -> None:
    from src.models.track_edit import clean_elevation_profile, elevation_gain
    from src.project.elevation_downsample import downsample_elevation
    from src.utils.encryption_check import is_encrypted_envelope

    def _store(profile):
        return (json.dumps({"distances_km": profile[0], "elevations_m": profile[1]})
                if profile else None)

    def _low_res(profile):
        return _store(downsample_elevation(*profile)) if profile else None

    def _figures(profile):
        """(gain, high, low) measured from a clean profile, or the empty ones."""
        if not profile:
            return 0.0, None, None
        dists, elevs = profile
        return float(elevation_gain(elevs, dists)), max(elevs), min(elevs)

    bind = op.get_bind()
    repaired_ids = []
    for row in bind.execute(sa.text(_CANDIDATES)).mappings().all():
        values = {c: row[c] for c in _PROFILES + _FIGURES}
        profiles = {}
        for col in ("elevation_profile_json", "original_elevation_profile_json"):
            text = row[col]
            if text is None or is_encrypted_envelope(text):
                continue
            ep = _parsed(text)
            if _broken(ep):
                profiles[col] = clean_elevation_profile(*ep)
                values[col] = _store(profiles[col])

        def _clean(col):
            """The column's profile as it stands after the repair, if readable."""
            if col in profiles:
                return profiles[col]
            text = row[col]
            if text is None or is_encrypted_envelope(text):
                return None
            return _parsed(text)

        current = _clean("elevation_profile_json")
        if "elevation_profile_json" in profiles:
            values["elevation_profile_low_res_json"] = _low_res(current)
        else:
            low = row["elevation_profile_low_res_json"]
            if low is not None and not is_encrypted_envelope(low) and _broken(_parsed(low)):
                values["elevation_profile_low_res_json"] = _low_res(current)

        gain, high, low_ = _figures(current)
        app_measured = row["source"] == "gpx" or bool(row["is_edited"])
        if "elevation_profile_json" in profiles and app_measured:
            values.update(total_elevation_gain=gain, elev_high=high, elev_low=low_)
        if not _finite(values["total_elevation_gain"]):
            values["total_elevation_gain"] = gain
        if not _finite(values["elev_high"]):
            values["elev_high"] = high
        if not _finite(values["elev_low"]):
            values["elev_low"] = low_
        if not _finite(values["original_total_elevation_gain"]):
            original = _clean("original_elevation_profile_json")
            values["original_total_elevation_gain"] = (
                _figures(original)[0] if original else None)

        changed = {c: v for c, v in values.items() if v != row[c]}
        if not changed:
            continue
        bind.execute(
            sa.text("UPDATE activity SET "
                    + ", ".join(f"{c} = :{c}" for c in changed) + " WHERE id = :id"),
            {**changed, "id": row["id"]},
        )
        repaired_ids.append(row["id"])

    _tell_the_trips(bind, repaired_ids)
    _log.info("non-finite elevation repair: %d activities repaired", len(repaired_ids))


def _tell_the_trips(bind, activity_ids: list) -> None:
    """Drop the cached totals of every trip holding a repaired row, and
    advance its lock_version.

    ``project.stats_json`` sums elevation across the trip and is recomputed
    only when NULL (see c4a9e1f70b38). The lock_version is what a client's
    on-device copy of the trip is checked against (issue #173): every writer
    of a trip's content advances it, and this is one.
    """
    for start in range(0, len(activity_ids), 500):   # keep the IN list sane
        chunk = activity_ids[start:start + 500]
        placeholders = ", ".join(f":a{i}" for i in range(len(chunk)))
        bind.execute(
            sa.text(
                "UPDATE project SET stats_json = NULL, lock_version = lock_version + 1 "
                "WHERE id IN (SELECT DISTINCT project_id FROM projectitem "
                f"WHERE activity_id IN ({placeholders}))"
            ),
            {f"a{i}": aid for i, aid in enumerate(chunk)},
        )


def downgrade() -> None:
    """No-op: the NaN and Infinity replaced are not worth restoring, and the
    code this reverts to would only write new ones."""
