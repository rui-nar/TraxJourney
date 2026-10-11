"""Each trip holds its own activity rows (#592, plan U3).

The same Strava activity in two trips is two rows, tied together by nothing but
``strava_id``. Editing, splitting, resetting or removing it in one trip leaves
the other trip's copy as it was, and a save of one trip never reads or writes a
row of another.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBActivityGeoPrepared, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.activity import Activity
from src.models.project import ProjectItem
from src.models.track_edit import TrackPoint
from src.project.local_ids import LocalIdExhausted, allocate_activity_id
from src.project.project_repo import ProjectRepo

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03), (48.0, 2.04)]
_ELEV = [100.0, 120.0, 110.0, 140.0, 130.0]
_POLY = polyline_lib.encode(_TRACK)
_STRAVA_ID = 9001
_ENVELOPE = "v1.d3JhcHBlZA==.Y2lwaGVydGV4dA=="

repo = ProjectRepo()


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _user(engine, name: str) -> int:
    with Session(engine) as sess:
        u = UserInfo(display_name=name, email=f"{name}@e.com")
        sess.add(u)
        sess.commit()
        return u.id


def _trip(engine, owner: int, name: str, activity_ids=()) -> int:
    with Session(engine) as sess:
        proj = DBProject(user_info_id=owner, name=name)
        sess.add(proj)
        sess.commit()
        for pos, aid in enumerate(activity_ids):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()
        return proj.id


def _row(engine, aid: int, **kw) -> None:
    """A plaintext Strava-style row with a five-point track."""
    fields = dict(
        user_info_id=kw.pop("owner"), name="Ride", type="Ride",
        distance=4000.0, moving_time=1000, elapsed_time=1200,
        total_elevation_gain=60.0, summary_polyline=_POLY,
        elevation_profile_json=json.dumps({
            "distances_km": [i * 1.0 for i in range(len(_TRACK))],
            "elevations_m": _ELEV}),
        start_latlng_json=json.dumps([48.0, 2.0]),
        end_latlng_json=json.dumps([48.0, 2.04]),
        start_date="2024-06-01T10:00:00Z", start_date_local="2024-06-01T12:00:00Z",
    )
    fields.update(kw)
    with Session(engine) as sess:
        sess.add(DBActivity(id=aid, **fields))
        sess.commit()


def _get(engine, aid: int) -> dict | None:
    with Session(engine) as sess:
        a = sess.get(DBActivity, aid)
        return None if a is None else {c: getattr(a, c) for c in DBActivity.model_fields}


def _items(engine, project_id: int) -> list[int]:
    with Session(engine) as sess:
        return [i.activity_id for i in sess.exec(
            select(DBProjectItem).where(DBProjectItem.project_id == project_id,
                                        DBProjectItem.item_type == "activity")
            .order_by(DBProjectItem.position))]


def _strava_activity(sid: int = _STRAVA_ID, name: str = "Morning ride") -> Activity:
    """An activity as a Strava sync or the add route hands it over: its id IS
    the Strava id."""
    return Activity.from_strava_api({
        "id": sid, "name": name, "type": "Ride",
        "start_date": "2024-06-01T08:00:00Z", "start_date_local": "2024-06-01T08:00:00Z",
        "start_latlng": [48.0, 2.0], "end_latlng": [48.0, 2.04],
        "map": {"summary_polyline": _POLY},
    })


def _add(engine, owner: int, trip: str, activities, adder: int | None = None):
    def _mutate(project):
        project.add_activities(activities)
    return repo.save_project_with_retry(owner, trip, _mutate, activity_user_id=adder)


def _two_trips_one_ride(engine):
    """Trips A and B of one owner, each holding its own copy of one Strava
    ride: row 1 in A, row 2 in B."""
    uid = _user(engine, "owner")
    for aid in (1, 2):
        _row(engine, aid, owner=uid, strava_id=_STRAVA_ID, strava_user_id=uid)
    a = _trip(engine, uid, "A", [1])
    b = _trip(engine, uid, "B", [2])
    return uid, a, b


# ── Allocation ─────────────────────────────────────────────────────────────────

class TestAllocateActivityId:
    def test_a_strava_row_gets_a_positive_id_and_a_local_row_a_negative_one(self, engine):
        with Session(engine) as sess:
            assert all(allocate_activity_id(sess, strava=True) > 0 for _ in range(20))
            assert all(allocate_activity_id(sess, strava=False) < 0 for _ in range(20))

    def test_both_signs_stay_inside_the_javascript_safe_range(self, engine):
        with Session(engine) as sess:
            for strava in (True, False):
                for _ in range(100):
                    assert abs(allocate_activity_id(sess, strava=strava)) <= 2 ** 53 - 1

    def test_a_positive_id_already_taken_is_redrawn(self, engine, monkeypatch):
        import src.project.local_ids as local_ids

        _row(engine, 42, owner=_user(engine, "u"))
        draws = iter([42, 0, 77])
        monkeypatch.setattr(local_ids.secrets, "randbits", lambda _bits: next(draws))
        with Session(engine) as sess:
            assert allocate_activity_id(sess, strava=True) == 77

    def test_zero_is_never_an_id(self, engine, monkeypatch):
        import src.project.local_ids as local_ids

        monkeypatch.setattr(local_ids.secrets, "randbits", lambda _bits: 0)
        with Session(engine) as sess, pytest.raises(LocalIdExhausted):
            allocate_activity_id(sess, strava=True)


# ── Adding activities: one row per trip ────────────────────────────────────────

def test_the_same_strava_activity_added_to_two_trips_makes_two_rows(engine):
    uid = _user(engine, "owner")
    a = _trip(engine, uid, "A")
    b = _trip(engine, uid, "B")

    _add(engine, uid, "A", [_strava_activity()])
    _add(engine, uid, "B", [_strava_activity()])

    (row_a,), (row_b,) = _items(engine, a), _items(engine, b)
    assert row_a != row_b
    for aid in (row_a, row_b):
        row = _get(engine, aid)
        assert aid > 0
        assert (row["strava_id"], row["strava_user_id"], row["user_info_id"]) == (
            _STRAVA_ID, uid, uid)
    with Session(engine) as sess:
        assert {p.activity_id for p in sess.exec(select(DBActivityGeoPrepared))} == {
            row_a, row_b}


def test_a_new_row_never_takes_the_strava_id_as_its_id(engine):
    """Ids are opaque (plan Decision 2): a new row's id is allocated, even
    when no row has the Strava id as its id."""
    uid = _user(engine, "owner")
    trip = _trip(engine, uid, "A")

    _add(engine, uid, "A", [_strava_activity()])

    (row_id,) = _items(engine, trip)
    assert row_id != _STRAVA_ID
    assert _get(engine, _STRAVA_ID) is None


def test_the_callers_activity_is_left_as_given(engine):
    """A retried save runs the mutation again on the same Activity objects:
    the save must not have rewritten their ids to the row it allocated."""
    uid = _user(engine, "owner")
    _trip(engine, uid, "A")
    act = _strava_activity()

    project = _add(engine, uid, "A", [act])

    assert act.id == _STRAVA_ID
    (saved,) = project.activities
    assert saved.id != _STRAVA_ID
    assert [it.activity_id for it in project.items if it.item_type == "activity"] == [saved.id]


def test_a_companions_activity_belongs_to_the_trip_owner(engine):
    """Plan Decisions 4 and 5: the trip owner owns the row, the companion's
    Strava account is the one that refreshes it."""
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    trip = _trip(engine, owner, "A")

    _add(engine, owner, "A", [_strava_activity()], adder=companion)

    (row_id,) = _items(engine, trip)
    row = _get(engine, row_id)
    assert (row["user_info_id"], row["strava_user_id"]) == (owner, companion)


def test_an_activity_carrying_an_envelope_stays_its_adders(engine):
    """Only the adder's key reads it (plan Decision 4, exception)."""
    owner = _user(engine, "owner")
    companion = _user(engine, "companion")
    trip = _trip(engine, owner, "A")
    act = _strava_activity()
    act.name = _ENVELOPE

    _add(engine, owner, "A", [act], adder=companion)

    (row_id,) = _items(engine, trip)
    assert _get(engine, row_id)["user_info_id"] == companion


def test_a_local_activity_gets_a_negative_row_and_no_strava_id(engine):
    uid = _user(engine, "owner")
    trip = _trip(engine, uid, "A")
    act = _strava_activity(sid=5)
    act.source, act.strava_id = "gpx", None

    _add(engine, uid, "A", [act])

    (row_id,) = _items(engine, trip)
    row = _get(engine, row_id)
    assert row_id < 0
    assert (row["strava_id"], row["strava_user_id"], row["source"]) == (None, None, "gpx")


def test_a_local_activity_keeps_the_id_its_caller_allocated(engine):
    """The GPX routes allocate the id first and answer it to the client."""
    uid = _user(engine, "owner")
    trip = _trip(engine, uid, "A")
    act = _strava_activity(sid=5)
    act.source, act.strava_id = "gpx", None
    with Session(engine) as sess:
        act.id = allocate_activity_id(sess, strava=False)

    _add(engine, uid, "A", [act])

    assert _items(engine, trip) == [act.id]


def test_an_activity_carrying_its_strava_id_is_matched_by_it(engine):
    """With ``strava_id`` set, the activity's own id says nothing."""
    uid = _user(engine, "owner")
    trip = _trip(engine, uid, "A")
    act = _strava_activity(sid=-3)
    act.strava_id = _STRAVA_ID

    _add(engine, uid, "A", [act])

    (row_id,) = _items(engine, trip)
    assert row_id > 0
    assert _get(engine, row_id)["strava_id"] == _STRAVA_ID


# ── Re-adding ──────────────────────────────────────────────────────────────────

def test_re_adding_an_activity_the_trip_holds_changes_nothing(engine):
    """A sync re-lists every Strava activity: the trip's own row stands for
    it, its name is kept, and the timeline gets no second item."""
    uid, a, _b = _two_trips_one_ride(engine)
    with Session(engine) as sess:
        row = sess.get(DBActivity, 1)
        row.name = "Ride (1/2)"
        row.split_base_name = "Ride"
        sess.commit()
    before = _get(engine, 1)

    _add(engine, uid, "A", [_strava_activity(name="Ride, renamed on Strava")])

    assert _items(engine, a) == [1]
    assert _get(engine, 1) == before
    with Session(engine) as sess:
        assert len(sess.exec(select(DBActivity).where(
            DBActivity.strava_id == _STRAVA_ID)).all()) == 2


def test_two_copies_in_one_add_make_one_row(engine):
    uid = _user(engine, "owner")
    trip = _trip(engine, uid, "A")
    act = _strava_activity()
    act.strava_id = _STRAVA_ID
    twin = _strava_activity(sid=7)
    twin.strava_id = _STRAVA_ID

    _add(engine, uid, "A", [act, twin])

    assert len(_items(engine, trip)) == 1


# ── save_project never reaches another trip's row ──────────────────────────────

def test_a_save_never_writes_or_references_another_trips_row(engine):
    uid, a, b = _two_trips_one_ride(engine)
    before = _get(engine, 2)
    with Session(engine) as sess:
        project = repo.get_project(sess, uid, "A")
        project.items.append(ProjectItem(item_type="activity", activity_id=2))
        repo.save_project(sess, uid, project)

    assert _items(engine, a) == [1]
    assert _items(engine, b) == [2]
    assert _get(engine, 2) == before


def test_a_save_carrying_another_trips_row_as_an_activity_copies_it(engine):
    """An activity the trip does not hold, named by its own id, is content
    to copy — never the other trip's row to take over or rewrite."""
    uid, a, b = _two_trips_one_ride(engine)
    _row(engine, -8, owner=uid, name="Walk", source="gpx")
    _trip(engine, uid, "C", [-8])
    before = _get(engine, -8)
    with Session(engine) as sess:
        walk = repo._row_to_activity(sess.get(DBActivity, -8))
        walk.name = "Walk, renamed"
        project = repo.get_project(sess, uid, "A")
        project.add_activities([walk])
        repo.save_project(sess, uid, project)

    assert _get(engine, -8) == before
    (copy_id,) = set(_items(engine, a)) - {1}
    assert copy_id < 0 and copy_id != -8
    assert _get(engine, copy_id)["name"] == "Walk, renamed"


def test_a_held_row_is_not_rewritten_by_a_save(engine):
    """What a save carries back was loaded from the row, and a light load
    carries no track at all: writing it back could only lose data."""
    uid, a, _b = _two_trips_one_ride(engine)
    before = _get(engine, 1)
    with Session(engine) as sess:
        project = repo.get_project(sess, uid, "A", include_heavy=False)
        project.activities[0].name = "Changed in memory"
        repo.save_project(sess, uid, project)

    after = _get(engine, 1)
    assert after == before


# ── Edit, split and reset in one trip leave the other's copy alone ─────────────

def _points():
    return [TrackPoint(lat, lng, 100.0) for lat, lng in _TRACK[:3]]


def test_editing_one_trips_copy_leaves_the_others(engine):
    uid, a, _b = _two_trips_one_ride(engine)
    before = _get(engine, 2)
    with Session(engine) as sess:
        assert repo.edit_activity_track(sess, a, 1, _points())

    assert _get(engine, 1)["is_edited"] is True
    assert _get(engine, 2) == before


def test_splitting_one_trips_copy_leaves_the_others(engine):
    uid, a, b = _two_trips_one_ride(engine)
    before = _get(engine, 2)
    with Session(engine) as sess:
        tail = repo.split_activity(sess, a, 1, 2)

    assert _items(engine, a) == [1, tail]
    assert _get(engine, 1)["name"] == "Ride (1/2)"
    assert _items(engine, b) == [2]
    assert _get(engine, 2) == before


def test_resetting_one_trips_split_leaves_the_others_copy(engine):
    uid, a, b = _two_trips_one_ride(engine)
    with Session(engine) as sess:
        repo.split_activity(sess, a, 1, 2)
        repo.split_activity(sess, b, 2, 2)
    before = _get(engine, 2)
    tails_b = set(_items(engine, b)) - {2}

    with Session(engine) as sess:
        assert repo.reset_activity_track(sess, a, 1)

    assert _items(engine, a) == [1]
    assert _get(engine, 1)["name"] == "Ride"
    assert _items(engine, b) == [2, *tails_b]
    assert _get(engine, 2) == before
    assert all(_get(engine, t) is not None for t in tails_b)


# ── Split walks stay inside the trip (review R1-2), seeded directly ────────────

def _cross_trip_family(engine):
    """Head 1 in trip A; tail -5 cut out of it but held by trip B, as a
    split link crossing trips (which the migration repairs) would leave it."""
    uid = _user(engine, "owner")
    _row(engine, 1, owner=uid, name="Ride (1/2)", split_base_name="Ride",
         is_edited=True, original_polyline=_POLY,
         original_elevation_profile_json=json.dumps({
             "distances_km": [i * 1.0 for i in range(len(_TRACK))],
             "elevations_m": _ELEV}),
         strava_id=_STRAVA_ID, strava_user_id=uid)
    _row(engine, -5, owner=uid, name="Ride (2/2)", split_root_id=1, split_parent_id=1,
         start_date="2024-06-01T11:00:00Z")
    a = _trip(engine, uid, "A", [1])
    b = _trip(engine, uid, "B", [-5])
    return uid, a, b


def test_a_reset_never_deletes_a_piece_another_trip_holds(engine):
    uid, a, b = _cross_trip_family(engine)
    before = _get(engine, -5)

    with Session(engine) as sess:
        assert repo.reset_activity_track(sess, a, 1)

    assert _get(engine, -5) == before
    assert _items(engine, b) == [-5]


def test_a_renumber_never_renames_a_piece_another_trip_holds(engine):
    uid, a, b = _cross_trip_family(engine)
    with Session(engine) as sess:
        tail = repo.split_activity(sess, a, 1, 2)

    assert _get(engine, -5)["name"] == "Ride (2/2)"
    assert _get(engine, 1)["name"] == "Ride (1/2)"
    assert _get(engine, tail)["name"] == "Ride (2/2)"


def test_a_renumber_in_the_tails_trip_leaves_another_trips_root(engine):
    uid, a, b = _cross_trip_family(engine)
    before = _get(engine, 1)
    with Session(engine) as sess:
        repo.split_activity(sess, b, -5, 2)

    assert _get(engine, 1) == before


def test_deleting_a_local_piece_never_reparents_another_trips_piece(engine):
    uid, a, b = _cross_trip_family(engine)
    _row(engine, -7, owner=uid, name="Ride (3/3)", split_root_id=1, split_parent_id=-5,
         start_date="2024-06-01T12:00:00Z")
    with Session(engine) as sess:
        sess.add(DBProjectItem(project_id=a, position=1, item_type="activity",
                               activity_id=-7))
        sess.commit()

    with Session(engine) as sess:
        assert repo.delete_local_activity(sess, b, -5)

    assert _get(engine, -7)["split_parent_id"] == -5


# ── Strava writes go to the given row only (plan Decision 6) ───────────────────

def test_a_refresh_writes_only_the_given_row(engine):
    uid, a, _b = _two_trips_one_ride(engine)
    before = _get(engine, 2)
    with Session(engine) as sess:
        repo.force_update_activity(sess, uid, _strava_activity(name="Fresh"), a, row_id=1)

    assert _get(engine, 1)["name"] == "Fresh"
    assert _get(engine, 2) == before


def test_a_refresh_of_a_row_the_trip_does_not_hold_writes_nothing(engine):
    uid, a, _b = _two_trips_one_ride(engine)
    before = _get(engine, 2)
    with Session(engine) as sess:
        repo.force_update_activity(sess, uid, _strava_activity(name="Fresh"), a, row_id=2)

    assert _get(engine, 2) == before


def test_a_refresh_through_another_strava_account_writes_nothing(engine):
    uid, a, _b = _two_trips_one_ride(engine)
    other = _user(engine, "other")
    before = _get(engine, 1)
    with Session(engine) as sess:
        repo.force_update_activity(sess, other, _strava_activity(name="X"), a, row_id=1)

    assert _get(engine, 1) == before


def test_a_refresh_whose_row_is_gone_creates_none(engine):
    uid = _user(engine, "owner")
    with Session(engine) as sess:
        repo.force_update_activity(sess, uid, _strava_activity())
    assert _get(engine, _STRAVA_ID) is None


def test_enrichment_writes_only_the_given_row(engine):
    uid, a, _b = _two_trips_one_ride(engine)
    before = _get(engine, 2)
    new_poly = polyline_lib.encode(_TRACK[:3])
    with Session(engine) as sess:
        repo.update_activity_enrichment(sess, 1, new_poly, None, owner_id=uid, project_id=a)
        repo.update_activity_enrichment(sess, 2, new_poly, None, owner_id=uid, project_id=a)

    assert _get(engine, 1)["summary_polyline"] == new_poly
    assert _get(engine, 2) == before


# ── Removing ───────────────────────────────────────────────────────────────────

def test_removing_it_from_one_trip_deletes_only_that_trips_row(engine):
    uid, a, b = _two_trips_one_ride(engine)
    with Session(engine) as sess:
        for it in sess.exec(select(DBProjectItem).where(DBProjectItem.project_id == a)):
            sess.delete(it)
        sess.commit()
        assert repo.delete_unreferenced_activities(sess, [1, 2]) == set()
        sess.commit()

    assert _get(engine, 1) is None
    assert _get(engine, 2) is not None
    assert _items(engine, b) == [2]


def test_the_rows_returned_as_renumbered_are_the_survivors_trips(engine):
    """A family that lost a piece is renamed in the trip holding it, whose
    lock_version moves so a cached copy notices."""
    uid = _user(engine, "owner")
    _row(engine, 1, owner=uid, name="Ride (1/3)", split_base_name="Ride", is_edited=True)
    _row(engine, -5, owner=uid, name="Ride (2/3)", split_root_id=1, split_parent_id=1,
         start_date="2024-06-01T11:00:00Z")
    _row(engine, -6, owner=uid, name="Ride (3/3)", split_root_id=1, split_parent_id=-5,
         start_date="2024-06-01T12:00:00Z")
    trip = _trip(engine, uid, "A", [1, -6])
    with Session(engine) as sess:
        version = sess.get(DBProject, trip).lock_version
        assert repo.delete_unreferenced_activities(sess, [-5]) == {trip}
        sess.commit()

    assert _get(engine, -5) is None
    assert _get(engine, -6)["split_parent_id"] == 1
    assert (_get(engine, 1)["name"], _get(engine, -6)["name"]) == ("Ride (1/2)", "Ride (2/2)")
    with Session(engine) as sess:
        assert sess.get(DBProject, trip).lock_version == version + 1


def test_a_root_a_held_piece_names_stays_until_its_last_piece_goes(engine):
    uid = _user(engine, "owner")
    _row(engine, 1, owner=uid, name="Ride (1/2)", split_base_name="Ride", is_edited=True)
    _row(engine, -5, owner=uid, name="Ride (2/2)", split_root_id=1, split_parent_id=1,
         start_date="2024-06-01T11:00:00Z")
    trip = _trip(engine, uid, "A", [-5])
    with Session(engine) as sess:
        repo.delete_unreferenced_activities(sess, [1])
        sess.commit()
    assert _get(engine, 1) is not None

    with Session(engine) as sess:
        for it in sess.exec(select(DBProjectItem).where(DBProjectItem.project_id == trip)):
            sess.delete(it)
        sess.commit()
        repo.delete_unreferenced_activities(sess, [-5, 1])
        sess.commit()
    assert _get(engine, 1) is None and _get(engine, -5) is None


# ── Over the API ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(engine, monkeypatch, tmp_path):
    import api.journal as journal_mod
    import api.memories as mem_mod
    import api.router as router

    for mod in (project_shared_mod, storage_mod, journal_mod, mem_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    uid = _user(engine, "alice")
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app), uid
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def test_adding_over_the_api_to_two_trips_gives_each_its_own_row(engine, client):
    http, uid = client
    body = {"activities": [{
        "id": _STRAVA_ID, "name": "Morning ride", "type": "Ride",
        "start_date": "2024-06-01T08:00:00Z", "start_date_local": "2024-06-01T08:00:00Z",
        "map": {"summary_polyline": _POLY}}]}
    for trip in ("One", "Two"):
        assert http.post("/api/projects/", json={"name": trip}).status_code == 201
        assert http.post(f"/api/projects/{trip}/activities", json=body).status_code == 200

    with Session(engine) as sess:
        rows = sess.exec(select(DBActivity)).all()
    assert len(rows) == 2
    assert {r.strava_id for r in rows} == {_STRAVA_ID}
    assert len({r.id for r in rows}) == 2


def test_a_trip_payload_carries_no_shared_with_others(engine, client):
    http, uid = client
    _row(engine, 1, owner=uid, strava_id=_STRAVA_ID, strava_user_id=uid)
    _trip(engine, uid, "A", [1])

    for path in ("/api/projects/A", "/api/projects/A/meta"):
        resp = http.get(path)
        assert resp.status_code == 200, resp.text
        assert "shared_with_others" not in resp.text


# ── Loading a row into the model ───────────────────────────────────────────────

def test_a_loaded_activity_carries_its_strava_id_split_family_and_snapshot(engine):
    """What a .traxj export writes (plan Decision 8): an edited split head
    with a plaintext snapshot, and a tail whose snapshot endpoints are
    envelopes, which load into the *_enc fields as the live ones do."""
    uid = _user(engine, "owner")
    profile = {"distances_km": [0.0, 1.0], "elevations_m": [100.0, 120.0]}
    _row(engine, 1, owner=uid, name="Ride (1/2)", split_base_name="Ride", is_edited=True,
         strava_id=_STRAVA_ID, strava_user_id=uid,
         original_polyline=_POLY, original_elevation_profile_json=json.dumps(profile),
         original_start_latlng_json="[48.0, 2.0]", original_end_latlng_json="[48.0, 2.04]",
         original_total_elevation_gain=60.0, original_distance=4000.0,
         original_moving_time=1000, original_elapsed_time=1200,
         original_average_speed=4.0, original_elev_high=140.0, original_elev_low=100.0)
    _row(engine, -5, owner=uid, name="Ride (2/2)", split_root_id=1, split_parent_id=1,
         is_edited=True, original_polyline=_ENVELOPE,
         original_elevation_profile_json=_ENVELOPE,
         original_start_latlng_json=_ENVELOPE, original_end_latlng_json=_ENVELOPE)
    _trip(engine, uid, "A", [1, -5])

    with Session(engine) as sess:
        acts = {a.id: a for a in repo.get_project(sess, uid, "A").activities}
        light = {a.id: a for a in repo.get_project(
            sess, uid, "A", include_heavy=False).activities}

    head, tail = acts[1], acts[-5]
    assert (head.strava_id, head.split_root_id, head.split_base_name) == (
        _STRAVA_ID, None, "Ride")
    assert head.original_polyline == _POLY
    assert head.original_elevation_profile == (profile["distances_km"], profile["elevations_m"])
    assert (head.original_start_latlng, head.original_end_latlng) == ([48.0, 2.0], [48.0, 2.04])
    assert head.original_elevation_profile_enc is None
    assert (head.original_total_elevation_gain, head.original_distance,
            head.original_moving_time, head.original_elapsed_time,
            head.original_average_speed, head.original_elev_high,
            head.original_elev_low) == (60.0, 4000.0, 1000, 1200, 4.0, 140.0, 100.0)

    assert (tail.strava_id, tail.split_root_id, tail.split_parent_id) == (None, 1, 1)
    assert tail.original_polyline == _ENVELOPE
    assert (tail.original_elevation_profile, tail.original_elevation_profile_enc) == (
        None, _ENVELOPE)
    assert (tail.original_start_latlng_enc, tail.original_end_latlng_enc) == (
        _ENVELOPE, _ENVELOPE)

    # The light path carries the identity and family, not the snapshot.
    assert (light[1].strava_id, light[1].split_base_name) == (_STRAVA_ID, "Ride")
    assert light[1].original_polyline is None
