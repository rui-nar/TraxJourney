"""API + repo integration tests for activity split + local-activity delete (#31, phase 2)."""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.activities import router as activities_router
from api.project_items import router as project_items_router
from api.projects import router as projects_router
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.track_edit import align_points, recompute_track_metrics

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]
_ELEV = [100.0, 120.0, 110.0, 140.0, 130.0]


def _seed(engine):
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="My Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)

        poly = polyline_lib.encode(_TRACK)
        dist_km = [i * 1.0 for i in range(len(_TRACK))]
        act = DBActivity(
            id=111, user_info_id=u.id, name="Ride", type="Ride",
            strava_id=111, strava_user_id=u.id,
            distance=4000.0, moving_time=1000, elapsed_time=1200,
            total_elevation_gain=60.0, summary_polyline=poly,
            elevation_profile_json=json.dumps({"distances_km": dist_km, "elevations_m": _ELEV}),
            start_latlng_json=json.dumps([48.0, 2.0]),
            end_latlng_json=json.dumps([48.0, 2.04]),
            start_date="2024-06-01T10:00:00Z",
            start_date_local="2024-06-01T12:00:00Z",
        )
        sess.add(act)
        # Sandwich the activity between two segments to verify item ordering.
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="segment", segment_json=json.dumps({"id": "s0"})))
        sess.add(DBProjectItem(project_id=proj.id, position=1,
                               item_type="activity", activity_id=111))
        sess.add(DBProjectItem(project_id=proj.id, position=2,
                               item_type="segment", segment_json=json.dumps({"id": "s1"})))
        sess.commit()
        return u.id


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    uid = _seed(engine)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@e.com"}
    app.include_router(projects_router)
    app.include_router(activities_router)
    app.include_router(project_items_router)
    return TestClient(app), engine


def test_split_yields_two_activities(env):
    client, engine = env
    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    acts = {a["id"]: a for a in body["activities"]}
    assert 111 in acts                       # head keeps its id
    tail_ids = [i for i in acts if i < 0]
    assert len(tail_ids) == 1                 # exactly one local tail
    tail = acts[tail_ids[0]]
    assert acts[111]["name"] == "Ride (1/2)"
    assert tail["name"] == "Ride (2/2)"
    assert tail["manual"] is True
    assert acts[111]["is_edited"] is True
    assert tail["is_edited"] is True


def test_split_reload_defers_full_elevation_profile(env, monkeypatch):
    """The split response is discarded by the client (it immediately re-fetches
    via /meta + /geo — see ProjectNotifier.splitActivity), so the endpoint's
    final reload must use include_elevation=False rather than pay to
    serialise every activity's full elevation_profile (~12 MB on a large trip)
    into a response nobody reads."""
    client, _ = env
    import api.activities as activities_mod
    seen = []
    orig = activities_mod._repo.get_project

    def _spy(sess, user_info_id, project_name, **kwargs):
        seen.append(kwargs)
        return orig(sess, user_info_id, project_name, **kwargs)

    monkeypatch.setattr(activities_mod._repo, "get_project", _spy)
    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    assert resp.status_code == 200, resp.text
    # The final reload (after the containment-check load) must defer elevation.
    assert seen[-1].get("include_elevation") is False


def test_split_distances_sum_to_original(env):
    client, _ = env
    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    acts = {a["id"]: a for a in resp.json()["activities"]}
    tail = next(a for i, a in acts.items() if i < 0)
    full = recompute_track_metrics(align_points(polyline_lib.encode(_TRACK), None)).distance
    assert acts[111]["distance"] + tail["distance"] == pytest.approx(full, rel=0.02)


def test_split_elevation_gain_sums_to_original(env):
    """The sibling of the distance property above, and the reason issue #386
    added apportioning: each piece used to be measured from scratch, so cutting
    a ride in two changed how much climb the trip contained. The pieces are
    shares of the parent's figure now, so they add back up to it."""
    client, engine = env
    with Session(engine) as sess:
        before = sess.get(DBActivity, 111).total_elevation_gain

    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    acts = {a["id"]: a for a in resp.json()["activities"]}
    tail = next(a for i, a in acts.items() if i < 0)

    assert acts[111]["total_elevation_gain"] + tail["total_elevation_gain"] ==         pytest.approx(before, rel=0.02)


def test_split_tail_starts_after_head(env):
    client, _ = env
    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    acts = {a["id"]: a for a in resp.json()["activities"]}
    head = acts[111]
    tail = next(a for i, a in acts.items() if i < 0)
    # The tail begins at the split boundary, strictly after the head's start, so
    # it sorts chronologically after the head (regression: it used to inherit the
    # head's start_date and sort before it).
    assert tail["start_date"] > head["start_date"]
    assert tail["start_date_local"] > head["start_date_local"]


def test_split_item_ordering(env):
    client, engine = env
    client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    resp = client.get("/api/projects/My Trip")
    items = resp.json()["items"]
    types = [it["item_type"] for it in items]
    assert types == ["segment", "activity", "activity", "segment"]
    # Head immediately precedes the tail; tail is the negative id.
    assert items[1]["activity_id"] == 111
    assert items[2]["activity_id"] < 0


def test_split_index_out_of_range(env):
    client, _ = env
    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 0})
    assert resp.status_code == 422
    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 99})
    assert resp.status_code == 422


def test_split_drop_boundary_excludes_point_from_tail(env):
    """#104: dropping the boundary point leaves the tail starting one point later,
    so the gap can be bridged by a transportation segment instead of overlapping.
    """
    client, _ = env
    resp = client.post(
        "/api/projects/My Trip/activities/111/split",
        json={"split_index": 2, "drop_boundary": True},
    )
    assert resp.status_code == 200, resp.text
    acts = {a["id"]: a for a in resp.json()["activities"]}
    tail = next(a for i, a in acts.items() if i < 0)
    # Head still ends at the boundary point (index 2 == (48.0, 2.02)).
    assert acts[111]["end_latlng"] == [48.0, 2.02]
    # Tail starts at index 3, not the shared boundary at index 2.
    assert tail["start_latlng"] == [48.0, 2.03]


def test_split_drop_boundary_requires_two_points_after_boundary(env):
    """The tail must retain >=2 points once the boundary point is excluded."""
    client, _ = env
    # _TRACK has 5 points (0..4); split_index=3 leaves only point 4 for the tail
    # once the boundary is dropped — too small.
    resp = client.post(
        "/api/projects/My Trip/activities/111/split",
        json={"split_index": 3, "drop_boundary": True},
    )
    assert resp.status_code == 422
    # split_index=2 still leaves 2 points (3, 4) for the tail — valid.
    resp = client.post(
        "/api/projects/My Trip/activities/111/split",
        json={"split_index": 2, "drop_boundary": True},
    )
    assert resp.status_code == 200, resp.text


# ── Splitting on top of unsaved editor edits (issue #127) ─────────────────────
# The track editor holds trims/deletes/moves locally until Save. A split driven
# from the stored geometry therefore discarded them AND applied split_index to a
# point list the user was not looking at, so the cut also landed in the wrong
# place. The client now sends its current points along with the split.

# The stored track with the middle point (2.02) deleted — the state on the
# user's screen, which the server has never been told about.
_EDITED = _TRACK[:2] + _TRACK[3:]


def _points_body(track):
    return [{"lat": lat, "lng": lng} for lat, lng in track]


def _decoded(engine, activity_id):
    with Session(engine) as sess:
        return polyline_lib.decode(sess.get(DBActivity, activity_id).summary_polyline)


def test_split_uses_supplied_edited_points(env):
    """A cut made on top of unsaved edits compounds with them (issue #127)."""
    client, engine = env
    resp = client.post(
        "/api/projects/My Trip/activities/111/split",
        json={"split_index": 1, "points": _points_body(_EDITED)},
    )
    assert resp.status_code == 200, resp.text
    tail_id = next(i for i in (a["id"] for a in resp.json()["activities"]) if i < 0)

    head_pts, tail_pts = _decoded(engine, 111), _decoded(engine, tail_id)
    # The deleted point survives in neither piece.
    assert all(lng != 2.02 for _, lng in head_pts + tail_pts)
    # And the cut landed at index 1 of the EDITED list, not of the stored one.
    assert head_pts == [(48.0, 2.0), (48.0, 2.01)]
    assert tail_pts == [(48.0, 2.01), (48.0, 2.03), (48.0, 2.04)]


def test_split_index_validated_against_supplied_points(env):
    """An index valid for the stored track but out of range for the shorter
    edited one is rejected, not silently applied to the stored geometry."""
    client, _ = env
    # The stored track has 5 points, so split_index=3 is fine there; the edited
    # list has 4, leaving only one point for the tail.
    resp = client.post(
        "/api/projects/My Trip/activities/111/split",
        json={"split_index": 3, "points": _points_body(_EDITED)},
    )
    assert resp.status_code == 422


def test_split_rejects_a_degenerate_supplied_track(env):
    client, _ = env
    resp = client.post(
        "/api/projects/My Trip/activities/111/split",
        json={"split_index": 1, "points": _points_body(_TRACK[:1])},
    )
    assert resp.status_code == 422


def test_split_without_points_uses_stored_track(env):
    """Omitting points keeps the pre-#127 behaviour — the split is taken from
    the stored geometry."""
    client, engine = env
    resp = client.post(
        "/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    assert resp.status_code == 200, resp.text
    tail_id = next(i for i in (a["id"] for a in resp.json()["activities"]) if i < 0)
    assert _decoded(engine, 111) == _TRACK[:3]
    assert _decoded(engine, tail_id) == _TRACK[2:]


def test_split_with_edited_points_apportions_times_against_the_stored_track(env):
    """Head and tail share ONE denominator — the stored track — so their times
    sum to the edited track's share of the original, with the trimmed section's
    share dropped rather than redistributed between the two pieces.

    Seeding the tail from the edited points instead would give it a smaller
    denominator than the head and inflate it.

    Uses a trim rather than _EDITED: deleting a collinear midpoint leaves the
    path length unchanged, so it cannot tell the two apportionings apart.
    """
    client, _ = env
    trimmed = _TRACK[:4]
    resp = client.post(
        "/api/projects/My Trip/activities/111/split",
        json={"split_index": 1, "points": _points_body(trimmed)},
    )
    assert resp.status_code == 200, resp.text
    acts = {a["id"]: a for a in resp.json()["activities"]}
    tail = next(a for i, a in acts.items() if i < 0)

    def _len(track):
        return recompute_track_metrics(
            align_points(polyline_lib.encode(track), None)).distance

    expected = 1000 * _len(trimmed) / _len(_TRACK)   # moving_time seeded at 1000
    assert acts[111]["moving_time"] + tail["moving_time"] == pytest.approx(
        expected, abs=2)


def test_delete_local_removes_row_and_item(env):
    client, engine = env
    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    tail_id = next(i for i in (a["id"] for a in resp.json()["activities"]) if i < 0)

    resp = client.delete(f"/api/projects/My Trip/activities/{tail_id}/local")
    assert resp.status_code == 204

    with Session(engine) as sess:
        assert sess.get(DBActivity, tail_id) is None
    # Head remains, tail item gone.
    body = client.get("/api/projects/My Trip").json()
    ids = [it.get("activity_id") for it in body["items"] if it["item_type"] == "activity"]
    assert ids == [111]


def test_delete_local_rejects_strava_id(env):
    client, _ = env
    # 111 comes from Strava (it has a strava_id) — not deletable through this
    # endpoint, which deletes only what the app made.
    resp = client.delete("/api/projects/My Trip/activities/111/local")
    assert resp.status_code == 404


def _seed_second_project(engine, user_info_id):
    """Add a second project with its own activity (id 222) for the same user."""
    with Session(engine) as sess:
        proj = DBProject(user_info_id=user_info_id, name="Trip Two")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        poly = polyline_lib.encode(_TRACK)
        dist_km = [i * 1.0 for i in range(len(_TRACK))]
        sess.add(DBActivity(
            id=222, user_info_id=user_info_id, name="Ride Two", type="Ride",
            distance=4000.0, moving_time=1000, elapsed_time=1200,
            total_elevation_gain=60.0, summary_polyline=poly,
            elevation_profile_json=json.dumps({"distances_km": dist_km, "elevations_m": _ELEV}),
            start_latlng_json=json.dumps([48.0, 2.0]),
            end_latlng_json=json.dumps([48.0, 2.04]),
            start_date="2024-06-01T10:00:00Z",
            start_date_local="2024-06-01T12:00:00Z",
        ))
        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="activity", activity_id=222))
        sess.commit()


def test_split_across_projects_allocates_distinct_ids(env):
    """Regression: tail ids must be globally unique.

    activity.id is a global PK. Allocating the negative tail id from only the
    current project's items reused -1 in a second project and hit
    'UNIQUE constraint failed: activity.id' on insert. The second split must
    get a distinct id, not collide.

    The ids themselves are drawn at random now (issue #260, unit 2) rather than
    counting down from -1, so this asserts the property — negative, and distinct
    — instead of the particular values the old allocator happened to produce.
    """
    client, engine = env
    with Session(engine) as sess:
        uid = sess.exec(select(UserInfo)).first().id   # user seeded by the fixture
    _seed_second_project(engine, uid)

    r1 = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    assert r1.status_code == 200, r1.text
    tail1 = next(i for i in (a["id"] for a in r1.json()["activities"]) if i < 0)
    assert tail1 < 0

    r2 = client.post("/api/projects/Trip Two/activities/222/split", json={"split_index": 2})
    assert r2.status_code == 200, r2.text
    tail2 = next(i for i in (a["id"] for a in r2.json()["activities"]) if i < 0)
    assert tail2 < 0
    assert tail2 != tail1                    # distinct global id, no collision


def test_resplit_renumbers_whole_family(env):
    """Splitting an already-split piece must renumber ALL surviving family
    members to reflect the new size (issue #45 follow-up) - not append another
    "(2)" suffix onto whatever name the piece already carries."""
    client, _ = env
    r1 = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    tail1_id = next(i for i in (a["id"] for a in r1.json()["activities"]) if i < 0)

    # Split the tail again (it has points[2:5] = 3 points, so index 1 is valid).
    r2 = client.post(f"/api/projects/My Trip/activities/{tail1_id}/split", json={"split_index": 1})
    assert r2.status_code == 200, r2.text
    acts = {a["id"]: a for a in r2.json()["activities"]}
    assert len(acts) == 3
    names = sorted(a["name"] for a in acts.values())
    assert names == ["Ride (1/3)", "Ride (2/3)", "Ride (3/3)"]
    # Chronological order matches the physical split order.
    by_start = sorted(acts.values(), key=lambda a: a["start_date"])
    assert [a["name"] for a in by_start] == ["Ride (1/3)", "Ride (2/3)", "Ride (3/3)"]


def test_delete_local_renumbers_survivors(env):
    """Deleting one piece of a 3-way split family must renumber the remaining
    two back down to (1/2)/(2/2), not leave stale (i/3) numbering."""
    client, _ = env
    r1 = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    tail1_id = next(i for i in (a["id"] for a in r1.json()["activities"]) if i < 0)
    r2 = client.post(f"/api/projects/My Trip/activities/{tail1_id}/split", json={"split_index": 1})
    acts = {a["id"]: a for a in r2.json()["activities"]}
    tail_ids = sorted(i for i in acts if i < 0)
    assert len(tail_ids) == 2

    resp = client.delete(f"/api/projects/My Trip/activities/{tail_ids[0]}/local")
    assert resp.status_code == 204

    body = client.get("/api/projects/My Trip").json()
    names = sorted(a["name"] for a in body["activities"])
    assert names == ["Ride (1/2)", "Ride (2/2)"]


def test_delete_local_restores_plain_name_when_family_reduced_to_one(env):
    """Deleting the only split tail leaves a single piece - it should get its
    plain base name back, not a stale "(1/1)"."""
    client, _ = env
    r1 = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    tail_id = next(i for i in (a["id"] for a in r1.json()["activities"]) if i < 0)

    resp = client.delete(f"/api/projects/My Trip/activities/{tail_id}/local")
    assert resp.status_code == 204

    body = client.get("/api/projects/My Trip").json()
    assert [a["name"] for a in body["activities"]] == ["Ride"]


def test_split_skips_renumbering_for_encrypted_name(env, monkeypatch):
    """An E2EE-encrypted activity name (issue #29) can't be used to derive a
    base name - split must leave names alone rather than corrupt ciphertext."""
    client, engine = env
    with Session(engine) as sess:
        act = sess.get(DBActivity, 111)
        act.name = "v1.abc.def"  # looks like a ciphertext envelope
        sess.add(act)
        sess.commit()

    resp = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    assert resp.status_code == 200, resp.text
    acts = {a["id"]: a for a in resp.json()["activities"]}
    assert acts[111]["name"] == "v1.abc.def"  # untouched


def test_delete_split_item_removes_local_row_and_allows_resplit(env):
    """Regression: deleting a split tail via the timeline must not orphan its row.

    Removing the tail's item with DELETE /items/{index} used to only unlink the
    item, leaving the negative-id activity row behind. The next split then reused
    that id and hit 'UNIQUE constraint failed: activity.id'. Deleting the item
    must now also delete the local row, so re-splitting succeeds.
    """
    client, engine = env
    r1 = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 2})
    tail_id = next(i for i in (a["id"] for a in r1.json()["activities"]) if i < 0)

    items = client.get("/api/projects/My Trip").json()["items"]
    tail_index = next(i for i, it in enumerate(items)
                      if it["item_type"] == "activity" and (it.get("activity_id") or 0) < 0)

    assert client.delete(f"/api/projects/My Trip/items/{tail_index}").status_code == 204

    # The local row is gone — not merely unlinked.
    with Session(engine) as sess:
        assert sess.get(DBActivity, tail_id) is None

    # Re-splitting the same head must succeed (previously collided on the orphan).
    # The head kept points[:3] from the first split, so split_index=1 is the only
    # in-range boundary now.
    r2 = client.post("/api/projects/My Trip/activities/111/split", json={"split_index": 1})
    assert r2.status_code == 200, r2.text


# ── Reset on a split tail (issue #131) ──────────────────────────────────────
#
# split_activity seeds the tail row with the head's FULL pre-split geometry so
# the tail's time apportioning has the right denominator. _write_track_geometry
# used to snapshot that seed into original_*, so "Reset" expanded the tail back
# into a duplicate of the whole track it was cut out of. The snapshot must be
# the tail's OWN post-split geometry instead.


def _split_and_get_tail(client):
    resp = client.post("/api/projects/My Trip/activities/111/split",
                       json={"split_index": 2})
    assert resp.status_code == 200, resp.text
    return next(i for i in (a["id"] for a in resp.json()["activities"]) if i < 0)


def test_split_tail_snapshots_its_own_geometry(env):
    client, engine = env
    tail_id = _split_and_get_tail(client)

    with Session(engine) as sess:
        tail = sess.get(DBActivity, tail_id)
        # The snapshot is the tail's own track, not the whole pre-split one.
        assert polyline_lib.decode(tail.original_polyline) == _TRACK[2:]
        assert tail.original_polyline == tail.summary_polyline
        assert tail.original_elevation_profile_json == tail.elevation_profile_json
        # The head still snapshots the full pre-split track: it IS the Strava
        # activity, so its own reset target is unchanged by this fix.
        assert sess.get(DBActivity, 111).original_polyline == polyline_lib.encode(_TRACK)


def test_reset_on_a_fresh_tail_is_a_noop(env):
    """A tail with no post-split edits has nothing to undo, so reset must leave
    its geometry, distance and times exactly as the split left them — it used to
    restore the whole pre-split track and its full duration."""
    client, engine = env
    tail_id = _split_and_get_tail(client)

    with Session(engine) as sess:
        before = sess.get(DBActivity, tail_id)
        poly, dist = before.summary_polyline, before.distance
        moving, elapsed = before.moving_time, before.elapsed_time

    resp = client.post(f"/api/projects/My Trip/activities/{tail_id}/reset")
    assert resp.status_code == 200, resp.text

    with Session(engine) as sess:
        after = sess.get(DBActivity, tail_id)
        assert after.summary_polyline == poly
        assert polyline_lib.decode(after.summary_polyline) == _TRACK[2:]
        assert after.distance == pytest.approx(dist, rel=1e-6)
        assert after.moving_time == pytest.approx(moving, abs=1)
        assert after.elapsed_time == pytest.approx(elapsed, abs=1)
        assert after.is_edited is False
        # And the head is untouched — the two pieces still tile the original
        # track rather than overlapping.
        assert polyline_lib.decode(
            sess.get(DBActivity, 111).summary_polyline) == _TRACK[:3]


def test_reset_on_an_edited_tail_restores_the_post_split_track(env):
    """Reset stays useful on a tail: it undoes edits made AFTER the split, back
    to the piece the split produced — not back to the whole pre-split track."""
    client, engine = env
    tail_id = _split_and_get_tail(client)

    # Trim the tail to its first two points.
    resp = client.put(f"/api/projects/My Trip/activities/{tail_id}/track",
                      json={"points": _points_body(_TRACK[2:4])})
    assert resp.status_code == 200, resp.text
    with Session(engine) as sess:
        assert polyline_lib.decode(
            sess.get(DBActivity, tail_id).summary_polyline) == _TRACK[2:4]

    resp = client.post(f"/api/projects/My Trip/activities/{tail_id}/reset")
    assert resp.status_code == 200, resp.text

    with Session(engine) as sess:
        row = sess.get(DBActivity, tail_id)
        assert polyline_lib.decode(row.summary_polyline) == _TRACK[2:]
        assert row.is_edited is False
        assert row.original_polyline is None


# ── Reset on a split root undoes the split (issue #141) ─────────────────────
#
# The root's snapshot IS the whole pre-split Strava track — correct in isolation,
# but restoring it while the pieces cut out of it survive leaves them sitting on
# top of that track. Reset on a root therefore removes the pieces and restores
# the plain pre-split name. The editor warns first (see the widget tests).


def _items(client):
    return client.get("/api/projects/My Trip").json()["items"]


def test_reset_on_a_split_root_removes_the_pieces(env):
    client, engine = env
    tail_id = _split_and_get_tail(client)

    resp = client.post("/api/projects/My Trip/activities/111/reset")
    assert resp.status_code == 200, resp.text

    with Session(engine) as sess:
        head = sess.get(DBActivity, 111)
        # The full Strava track is back...
        assert polyline_lib.decode(head.summary_polyline) == _TRACK
        assert head.is_edited is False
        # ...and nothing overlaps it: the tail row is gone, not merely unlinked.
        assert sess.get(DBActivity, tail_id) is None

    # Its timeline item went with it, and the trip is back to its pre-split shape.
    assert [it["item_type"] for it in _items(client)] == [
        "segment", "activity", "segment"]
    with Session(engine) as sess:
        positions = sess.exec(
            select(DBProjectItem.position).order_by(DBProjectItem.position)).all()
        assert positions == list(range(len(positions)))   # stayed contiguous


def test_reset_on_a_split_root_restores_the_plain_name(env):
    client, engine = env
    _split_and_get_tail(client)
    with Session(engine) as sess:
        assert sess.get(DBActivity, 111).name == "Ride (1/2)"

    assert client.post("/api/projects/My Trip/activities/111/reset").status_code == 200

    with Session(engine) as sess:
        # "(1/2)" is meaningless once it is no longer one of two.
        assert sess.get(DBActivity, 111).name == "Ride"


def _split_twice(client, engine):
    """111 → [111, first] → [111, first, second], second cut out of first."""
    first = _split_and_get_tail(client)
    resp = client.post(f"/api/projects/My Trip/activities/{first}/split",
                       json={"split_index": 1})
    assert resp.status_code == 200, resp.text
    second = next(i for i in (a["id"] for a in resp.json()["activities"])
                  if i < 0 and i != first)
    with Session(engine) as sess:
        # The whole family shares one root, but the parents form the real chain.
        assert sess.get(DBActivity, second).split_root_id == 111
        assert sess.get(DBActivity, first).split_parent_id == 111
        assert sess.get(DBActivity, second).split_parent_id == first
    return first, second


def test_reset_on_a_split_root_removes_pieces_split_out_of_pieces(env):
    """The walk is transitive, so a grandchild goes too — not left stranded
    below a root that has been restored over the top of it."""
    client, engine = env
    first_tail, second_tail = _split_twice(client, engine)

    assert client.post("/api/projects/My Trip/activities/111/reset").status_code == 200

    with Session(engine) as sess:
        assert sess.get(DBActivity, first_tail) is None
        assert sess.get(DBActivity, second_tail) is None
        assert polyline_lib.decode(sess.get(DBActivity, 111).summary_polyline) == _TRACK


def test_reset_on_a_resplit_piece_removes_its_own_children_only(env):
    """Issue #143. Resetting the middle piece restores the span it covered before
    it was cut again — which is exactly where its child sits — so the child goes
    with it. Its parent, the root, is untouched: it is not below the reset."""
    client, engine = env
    first_tail, second_tail = _split_twice(client, engine)
    with Session(engine) as sess:
        assert polyline_lib.decode(
            sess.get(DBActivity, first_tail).summary_polyline) == _TRACK[2:4]

    assert client.post(
        f"/api/projects/My Trip/activities/{first_tail}/reset").status_code == 200

    with Session(engine) as sess:
        # The middle piece grew back over its child's span...
        assert polyline_lib.decode(
            sess.get(DBActivity, first_tail).summary_polyline) == _TRACK[2:]
        # ...so the child must be gone rather than duplicated underneath it.
        assert sess.get(DBActivity, second_tail) is None
        # The root above the reset is untouched, and the family renumbered.
        assert polyline_lib.decode(
            sess.get(DBActivity, 111).summary_polyline) == _TRACK[:3]
        assert sess.get(DBActivity, 111).name == "Ride (1/2)"
        assert sess.get(DBActivity, first_tail).name == "Ride (2/2)"


def test_deleting_a_middle_piece_reparents_its_children(env):
    """A deleted piece's children are adopted by its own parent, so the chain
    stays connected — otherwise they are unreachable from any ancestor and a
    later reset restores a track straight over the top of them (#143)."""
    client, engine = env
    first_tail, second_tail = _split_twice(client, engine)

    items = _items(client)
    idx = next(i for i, it in enumerate(items)
               if it.get("activity_id") == first_tail)
    assert client.delete(f"/api/projects/My Trip/items/{idx}").status_code == 204

    with Session(engine) as sess:
        # The grandchild now hangs off the root directly.
        assert sess.get(DBActivity, second_tail).split_parent_id == 111

    # ...so resetting the root still reaches it.
    assert client.post("/api/projects/My Trip/activities/111/reset").status_code == 200
    with Session(engine) as sess:
        assert sess.get(DBActivity, second_tail) is None


def test_reset_on_a_tail_leaves_its_siblings_alone(env):
    """A piece with nothing cut out of it cascades nothing: resetting it undoes
    its own post-split edits (#131) and leaves the rest of the family alone."""
    client, engine = env
    tail_id = _split_and_get_tail(client)

    assert client.post(
        f"/api/projects/My Trip/activities/{tail_id}/reset").status_code == 200

    with Session(engine) as sess:
        # Both pieces still there, still named as a family of two.
        assert sess.get(DBActivity, tail_id) is not None
        assert sess.get(DBActivity, 111).name == "Ride (1/2)"
        assert polyline_lib.decode(
            sess.get(DBActivity, 111).summary_polyline) == _TRACK[:3]


def test_reset_on_an_unsplit_activity_touches_no_items(env):
    """The cascade must not fire for an ordinary edited activity — it has no
    family, so reset is still just a geometry restore."""
    client, engine = env
    before = len(_items(client))
    resp = client.put("/api/projects/My Trip/activities/111/track",
                      json={"points": _points_body(_TRACK[:3])})
    assert resp.status_code == 200, resp.text

    assert client.post("/api/projects/My Trip/activities/111/reset").status_code == 200

    assert len(_items(client)) == before
    with Session(engine) as sess:
        row = sess.get(DBActivity, 111)
        assert polyline_lib.decode(row.summary_polyline) == _TRACK
        assert row.name == "Ride"


def test_split_parent_id_reaches_the_client(env):
    """The editor walks this field to decide whether to warn, so it has to
    survive serialisation — a piece carries its parent's id, the root None."""
    client, _ = env
    resp = client.post("/api/projects/My Trip/activities/111/split",
                       json={"split_index": 2})
    acts = {a["id"]: a for a in resp.json()["activities"]}
    tail = next(a for i, a in acts.items() if i < 0)
    assert acts[111]["split_parent_id"] is None
    assert tail["split_parent_id"] == 111
    # split_root_id stays DB-only: it answers "which family", not "what is below
    # this piece", which is the question the warning needs (#143).
    assert "split_root_id" not in tail
