"""repair implausible elevations and elapsed times stored before #462

Until #462 the app stored what it was given. gpxpy reads ``<ele>NaN</ele>``
and ``<ele>inf</ele>`` as floats, devices write ``<ele>65535</ele>`` for no
reading, and nothing checked either (nor the track editor, nor the Strava
stream code): an activity could store a NaN, an Infinity or an elevation far
past any on Earth in its elevation profile, and in its gain and high/low
(SQLite stores a NaN REAL as NULL). And one stray GPX timestamp (a clock that
read 1970) made an activity's elapsed time decades long. The trip's JSON then
held tokens the client's parser refuses, or its export held figures the
import's bounds refuse.

The writers now treat such a value as missing, and the import normalises it
the same way. This repairs what is stored, through the very code both use
(``repair_elevations`` and ``repair_elapsed`` in src/models/track_edit.py,
imported from the app as c4a9e1f70b38 and b7f1a3c9d204 do), so an old row
and an old export of it come out alike:

* a profile, and the edit-undo snapshot of one (which a reset restores
  verbatim), is rebuilt by ``clean_elevation_profile``: an implausible
  reading is filled by distance like a missing ``<ele>``, a sample with no
  usable distance is dropped, and a profile left with no reading becomes
  NULL. The low-res copy the chart loads first is rebuilt from it;
* on an uploaded GPX track, whose figures the app measured itself from the
  same readings, gain, high and low are measured again from the repaired
  profile, and so is the gain its undo snapshot keeps; so on a piece split
  out of one, whose source is its family's when it has none of its own. A
  Strava activity keeps Strava's own figures, and an edited one its share
  of them (#386);
* a figure still implausible is measured from the profile if there is one,
  and otherwise cleared: gain to 0.0 (its NOT NULL column's default), high
  and low to NULL, the snapshot's gain to NULL (a reset then remeasures it);
* an elapsed time past 31 years becomes the moving time;
* every trip holding a repaired row has its cached totals dropped and its
  lock_version advanced, so a client's on-device copy is refetched (#173);
  a trip whose cached totals overflowed to Infinity on their own (summing
  huge but finite figures) has them dropped too.

A profile that is a client-side E2EE envelope is never selected: the server
holds no key for it (issue #366). Candidates are found in SQL and read one at
a time by id, since this runs at startup and a profile can be megabytes.

Idempotent: a repaired row holds nothing to repair, so a second run changes
nothing. The downgrade does nothing: the values replaced are not worth
restoring, and the old code would only write new ones.

Revision ID: 6c1f0e9a2b47
Revises: 5e2b7c1d9a40
Create Date: 2026-09-26

"""
import json
import logging
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
            "original_total_elevation_gain", "moving_time", "elapsed_time")

#: The bounds of src/models/value_bounds.py, restated for the scan: a
#: migration's selection must not change when the app's constants do.
_ELEVATION_M = 20_000
_GAIN_MAX_M = 1e7
_DURATION_MAX_S = 1e9


def _candidate_ids(bind) -> list:
    """The ids of the rows to look at, found in SQL so that no clean or
    encrypted profile is read into Python.

    A plaintext profile is a JSON object, so it starts with ``{``; an E2EE
    envelope starts with ``v1.`` and is never selected. Within a plaintext
    profile, on SQLite: the tokens json.dumps writes for a non-finite float,
    ``null``, a number of five or more integer digits (an elevation past
    ±20 km has at least five; a decimal part never follows a space, comma or
    bracket), or an exponent (json.dumps writes a float from 1e16 up as
    ``1e+16``). Some of those are plausible (12,345 m, a 10,000 km distance):
    the row is then read and left as it is. On any other database every
    plaintext profile is read. The figures are compared in SQL: a REAL past
    the bounds, or Infinity (on PostgreSQL NaN also sorts above them).
    """
    if bind.dialect.name == "sqlite":
        def suspect(c):
            return (f"{c} GLOB '{{*' AND ({c} GLOB '*NaN*' OR {c} GLOB '*Infinity*'"
                    f" OR {c} GLOB '*null*' OR {c} GLOB '*[ ,[-][0-9][0-9][0-9][0-9][0-9]*'"
                    f" OR {c} GLOB '*[0-9]e+*')")
    else:
        def suspect(c):
            return f"{c} LIKE '{{%'"
    profiles = " OR ".join(f"({suspect(c)})" for c in _PROFILES)
    figures = " OR ".join([
        *(f"{c} > {_ELEVATION_M} OR {c} < -{_ELEVATION_M}" for c in ("elev_high", "elev_low")),
        *(f"{c} > {_GAIN_MAX_M} OR {c} < 0"
          for c in ("total_elevation_gain", "original_total_elevation_gain")),
        f"elapsed_time > {_DURATION_MAX_S}",
    ])
    return [r[0] for r in bind.execute(sa.text(
        f"SELECT id FROM activity WHERE {profiles} OR {figures} ORDER BY id"))]


_ROW = ("SELECT id, source, split_root_id, split_parent_id, "
        + ", ".join(_PROFILES + _FIGURES) + " FROM activity WHERE id = :id")


def _app_measured(bind, row) -> bool:
    """Whether the app measured the row's figures: a GPX upload, or a piece
    split out of one. Tails split before #462 have no source of their own, so
    theirs is their family's: its root, else up the chain of parents."""
    source, seen = row["source"], set()
    parent = row["split_root_id"] or row["split_parent_id"]
    while source is None and parent is not None and parent not in seen:
        seen.add(parent)
        found = bind.execute(sa.text(
            "SELECT source, split_parent_id FROM activity WHERE id = :id"),
            {"id": parent}).first()
        if found is None:
            break
        source, parent = found
    return source == "gpx"


def _parsed(ep_json):
    """``(distances, elevations)`` of a stored plaintext profile, or None."""
    try:
        ep = json.loads(ep_json)
        return (ep.get("distances_km") or []), (ep.get("elevations_m") or [])
    except (ValueError, TypeError, AttributeError):
        return None


def upgrade() -> None:
    from src.models.track_edit import (
        profile_needs_repair, repair_elapsed, repair_elevations,
    )
    from src.project.elevation_downsample import downsample_elevation
    from src.utils.encryption_check import is_encrypted_envelope

    def _store(profile):
        return (json.dumps({"distances_km": profile[0], "elevations_m": profile[1]})
                if profile else None)

    def _readable(text):
        if text is None or is_encrypted_envelope(text):
            return None
        return _parsed(text)

    bind = op.get_bind()
    repaired_ids = []
    # One row at a time: this runs at startup, and a profile can be megabytes.
    for row_id in _candidate_ids(bind):
        row = bind.execute(sa.text(_ROW), {"id": row_id}).mappings().one()
        values = {c: row[c] for c in _PROFILES + _FIGURES}
        app_measured = _app_measured(bind, row)

        current = _readable(row["elevation_profile_json"])
        profile, gain, high, low = repair_elevations(
            current, row["total_elevation_gain"], row["elev_high"], row["elev_low"],
            app_measured=app_measured)
        values.update(total_elevation_gain=gain, elev_high=high, elev_low=low)
        low_res = None
        if profile is current:
            # Read only when the profile it copies stands: a repaired profile
            # gets a new copy anyway.
            low_res = _readable(row["elevation_profile_low_res_json"])
        if profile is not current or (low_res is not None and profile_needs_repair(low_res)):
            values["elevation_profile_low_res_json"] = (
                _store(downsample_elevation(*profile)) if profile else None)
        if profile is not current:
            values["elevation_profile_json"] = _store(profile)
        del current, profile, low_res

        original = _readable(row["original_elevation_profile_json"])
        o_profile, o_gain, _, _ = repair_elevations(
            original, row["original_total_elevation_gain"], None, None,
            app_measured=app_measured)
        if o_profile is not original:
            values["original_elevation_profile_json"] = _store(o_profile)
        values["original_total_elevation_gain"] = o_gain

        values["elapsed_time"] = repair_elapsed(row["elapsed_time"], row["moving_time"])

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
    # Cached totals can overflow on their own, summing huge but finite
    # figures: a cache, so dropping it is enough (readers recompute on NULL).
    has = "GLOB '*{}*'" if bind.dialect.name == "sqlite" else "LIKE '%{}%'"
    bind.execute(sa.text(
        "UPDATE project SET stats_json = NULL WHERE stats_json "
        + has.format("Infinity") + " OR stats_json " + has.format("NaN")))
    _log.info("implausible elevation repair: %d activities repaired", len(repaired_ids))


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
    """No-op: the values replaced are not worth restoring, and the code this
    reverts to would only write new ones."""
