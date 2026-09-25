"""An activity row belongs to one account, and trips only reach it legitimately.

Activity rows are keyed by their Strava id and record the account that owns
them (``user_info_id``). A trip reaches an activity through its items, so which
activities a trip may reference decides what every read path returns: the
owner's and their companions' views, public share links, geo, stats, exports.

These tests pin that adding activities to a trip (a ``.traxj`` import, the
add-activities endpoint, a Strava sync) never modifies a row another account
owns and never makes that row part of the caller's trip, while activities
legitimately shared through a trip stay visible to its companions and its
public share link.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.project_io import ProjectIO

_VICTIM_ACT = 9001
_POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
_NAME = "Morning ride"


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.journal as journal_mod
    import api.memories as mem_mod
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, storage_mod, journal_mod, mem_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com")
                 for n in ("alice", "bob", "carol")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}

    current = {"uid": ids["alice"]}
    router.app.dependency_overrides[get_current_user] = (
        lambda: {"sub": str(current["uid"])})

    def act_as(who: str) -> None:
        current["uid"] = ids[who]

    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine, ids, act_as
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _activity(aid: int, name: str = _NAME, polyline: str = _POLYLINE) -> dict:
    return {
        "id": aid, "name": name, "type": "Ride",
        "start_date": "2024-06-01T08:00:00Z",
        "start_date_local": "2024-06-01T08:00:00Z",
        "start_latlng": [48.0, 2.0], "end_latlng": [48.1, 2.1],
        "map": {"summary_polyline": polyline},
    }


def _create_trip(client, name: str, owner_q: str = "") -> None:
    r = client.post("/api/projects/", json={"name": name})
    assert r.status_code == 201, r.text


def _add(client, trip: str, activities: list, owner_q: str = ""):
    return client.post(f"/api/projects/{trip}/activities{owner_q}",
                       json={"activities": activities})


@pytest.fixture
def bobs_ride(env):
    """Bob owns activity 9001, in his own trip."""
    client, engine, ids, act_as = env
    act_as("bob")
    _create_trip(client, "Bob trip")
    r = _add(client, "Bob trip", [_activity(_VICTIM_ACT)])
    assert r.status_code == 200, r.text
    act_as("alice")
    return env


def _row(engine, aid: int) -> DBActivity:
    with Session(engine) as sess:
        row = sess.get(DBActivity, aid)
        sess.expunge_all()
        return row


def _assert_bobs_row_untouched(engine, ids) -> None:
    row = _row(engine, _VICTIM_ACT)
    assert row.user_info_id == ids["bob"]
    assert row.name == _NAME
    assert row.summary_polyline == _POLYLINE


def _trip_activity_ids(engine, trip_owner: int, trip: str) -> set[int]:
    with Session(engine) as sess:
        pid = sess.exec(select(DBProject.id).where(
            DBProject.user_info_id == trip_owner, DBProject.name == trip)).one()
        return {i.activity_id for i in sess.exec(select(DBProjectItem).where(
            DBProjectItem.project_id == pid,
            DBProjectItem.item_type == "activity"))}


def _assert_not_exposed(client, trip: str) -> None:
    """None of the read paths of *trip* returns Bob's activity."""
    paths = [f"/api/projects/{trip}{p}" for p in ("", "/meta", "/stats", "/export-traxj")]
    paths += [f"/api/geo/project{p}?name={trip}" for p in ("", "/low-res")]
    paths.append(f"/api/geo/project/simplified?name={trip}&zoom=10")
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert _POLYLINE not in r.text, path
        assert _NAME not in r.text, path
    r = client.get(f"/api/projects/{trip}/activities/{_VICTIM_ACT}/track")
    assert r.status_code == 404, r.text


# ── Adding someone else's activity id ───────────────────────────────────────

def test_an_import_naming_another_accounts_activity_neither_changes_nor_exposes_it(bobs_ride):
    client, engine, ids, act_as = bobs_ride
    doc = {
        "version": 1, "name": "x", "items": [
            {"item_type": "activity", "activity_id": _VICTIM_ACT}],
        "activities": [_activity(_VICTIM_ACT, name="Renamed", polyline="??")],
    }

    r = client.post("/api/projects/import", files={
        "file": (f"Mine{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 201, r.text
    _assert_bobs_row_untouched(engine, ids)
    assert _VICTIM_ACT not in _trip_activity_ids(engine, ids["alice"], "Mine")
    _assert_not_exposed(client, "Mine")
    # The file's own activity is kept, as the importer's own local copy.
    (copy_id,) = _trip_activity_ids(engine, ids["alice"], "Mine")
    copy = _row(engine, copy_id)
    assert copy_id < 0
    assert (copy.user_info_id, copy.name, copy.summary_polyline) == (
        ids["alice"], "Renamed", "??")


def test_an_import_item_naming_another_accounts_activity_it_does_not_carry_is_left_out(bobs_ride):
    client, engine, ids, act_as = bobs_ride
    doc = {"version": 1, "name": "x",
           "items": [{"item_type": "activity", "activity_id": _VICTIM_ACT}],
           "activities": []}

    r = client.post("/api/projects/import", files={
        "file": (f"Mine{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 201, r.text
    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()
    _assert_not_exposed(client, "Mine")


def test_an_import_of_a_local_activity_id_another_account_uses_gets_a_new_id(env):
    """Local (negative) ids are app-made; an exported file carries its
    exporter's, which may be taken by someone else here."""
    client, engine, ids, act_as = env
    with Session(engine) as sess:
        sess.add(DBActivity(id=-77, user_info_id=ids["bob"], name="Bob GPX",
                            summary_polyline=_POLYLINE, source="gpx"))
        sess.commit()
    doc = {"version": 1, "name": "x",
           "items": [{"item_type": "activity", "activity_id": -77}],
           "activities": [_activity(-77, name="Alice GPX", polyline="??")]}

    r = client.post("/api/projects/import", files={
        "file": (f"Mine{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 201, r.text
    (new_id,) = _trip_activity_ids(engine, ids["alice"], "Mine")
    assert new_id < 0 and new_id != -77
    assert _row(engine, new_id).name == "Alice GPX"
    assert _row(engine, -77).name == "Bob GPX"
    r = client.get("/api/projects/Mine")
    assert r.status_code == 200
    assert "Bob GPX" not in r.text


def test_an_import_of_the_importers_own_activity_reuses_it(env):
    client, engine, ids, act_as = env
    _create_trip(client, "One")
    assert _add(client, "One", [_activity(5001)]).json()["added"] == 1
    doc = {"version": 1, "name": "x",
           "items": [{"item_type": "activity", "activity_id": 5001}],
           "activities": [_activity(5001)]}

    r = client.post("/api/projects/import", files={
        "file": (f"Two{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 201, r.text
    assert _trip_activity_ids(engine, ids["alice"], "Two") == {5001}


def test_adding_another_accounts_activity_neither_changes_nor_exposes_it(bobs_ride):
    client, engine, ids, act_as = bobs_ride
    _create_trip(client, "Mine")

    r = _add(client, "Mine", [_activity(_VICTIM_ACT, name="Renamed", polyline="??")])

    assert r.status_code == 200, r.text
    assert r.json()["added"] == 0
    _assert_bobs_row_untouched(engine, ids)
    assert _VICTIM_ACT not in _trip_activity_ids(engine, ids["alice"], "Mine")
    _assert_not_exposed(client, "Mine")


def test_a_strava_sync_listing_another_accounts_activity_leaves_it_alone(
        bobs_ride, monkeypatch):
    """Two accounts linked to one Strava athlete see the same ids."""
    import api.strava as strava_mod
    from models.user import StravaToken

    client, engine, ids, act_as = bobs_ride
    _create_trip(client, "Mine")
    with Session(engine) as sess:
        sess.add(StravaToken(user_info_id=ids["alice"], access_token="t",
                             refresh_token="r", expires_at=9e12))
        sess.commit()
    monkeypatch.setattr(strava_mod, "_strava_client_for_token", lambda _row: object())
    monkeypatch.setattr(strava_mod, "_fetch_all_strava", lambda _c: [
        _activity(_VICTIM_ACT, name="Renamed", polyline="??"),
        _activity(9002, name="Alice's own"),
    ])
    monkeypatch.setattr(strava_mod, "_save_cache", lambda *_a: None)
    monkeypatch.setattr(strava_mod, "_save_refreshed_token", lambda *_a: None)

    r = client.post("/api/projects/Mine/strava/sync")

    assert r.status_code == 200, r.text
    _assert_bobs_row_untouched(engine, ids)
    assert _trip_activity_ids(engine, ids["alice"], "Mine") == {9002}
    assert _row(engine, 9002).user_info_id == ids["alice"]


def test_a_refresh_never_moves_an_activity_to_another_account(env):
    """force_update_activity used to rewrite the row's owner to the caller."""
    client, engine, ids, act_as = env
    from src.models.activity import Activity
    act_as("bob")
    _create_trip(client, "Bob trip")
    assert _add(client, "Bob trip", [_activity(_VICTIM_ACT)]).status_code == 200

    with Session(engine) as sess:
        project_shared_mod._repo.force_update_activity(
            sess, ids["alice"], Activity.from_strava_api(_activity(_VICTIM_ACT, name="X")))

    row = _row(engine, _VICTIM_ACT)
    assert row.user_info_id == ids["bob"]
    assert row.name == _NAME


# ── Legitimate sharing keeps working ────────────────────────────────────────

def _join(client, act_as, owner: str, member: str, trip: str) -> str:
    act_as(owner)
    token = client.post(f"/api/projects/{trip}/members/invite").json()["token"]
    act_as(member)
    assert client.post(f"/api/invites/{token}/accept").status_code == 200


def test_a_companions_activity_is_visible_to_the_owner_and_the_share_link(env):
    client, engine, ids, act_as = env
    act_as("alice")
    _create_trip(client, "Japan")
    _join(client, act_as, "alice", "carol", "Japan")
    owner_q = f"?owner={ids['alice']}"

    act_as("carol")
    r = _add(client, "Japan", [_activity(7001, name="Carol's ride")], owner_q)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1
    assert _row(engine, 7001).user_info_id == ids["carol"]

    act_as("alice")
    assert "Carol's ride" in client.get("/api/projects/Japan").text
    token = client.post("/api/projects/Japan/share").json()["share_token"]
    assert "Carol's ride" in client.get(f"/api/share/{token}").text

    # The owner re-saving the trip (a reorder here) keeps Carol's row Carol's.
    r = client.put("/api/projects/Japan/items/sort")
    assert r.status_code == 204, r.text
    assert _row(engine, 7001).user_info_id == ids["carol"]

    # And a companion who leaves leaves their contribution behind, as with
    # memories (test_companion_e2e).
    act_as("carol")
    assert client.delete(
        f"/api/projects/Japan/members/{ids['carol']}{owner_q}").status_code == 204
    act_as("alice")
    assert "Carol's ride" in client.get("/api/projects/Japan").text


def test_an_account_can_add_its_own_activity_to_several_trips(env):
    client, engine, ids, act_as = env
    _create_trip(client, "One")
    _create_trip(client, "Two")

    assert _add(client, "One", [_activity(5001)]).json()["added"] == 1
    assert _add(client, "Two", [_activity(5001)]).json()["added"] == 1

    assert _VICTIM_ACT not in _trip_activity_ids(engine, ids["alice"], "Two")
    assert 5001 in _trip_activity_ids(engine, ids["alice"], "Two")


def test_a_companion_can_add_the_owners_activity_already_in_the_trip_as_a_no_op(env):
    """Re-adding what the trip already holds is not taking someone's row."""
    client, engine, ids, act_as = env
    _create_trip(client, "Japan")
    assert _add(client, "Japan", [_activity(5001)]).json()["added"] == 1
    _join(client, act_as, "alice", "carol", "Japan")

    act_as("carol")
    r = _add(client, "Japan", [_activity(5001, name="Renamed")], f"?owner={ids['alice']}")

    assert r.status_code == 200, r.text
    assert r.json()["added"] == 0
    row = _row(engine, 5001)
    assert (row.user_info_id, row.name) == (ids["alice"], _NAME)


# ── The writers themselves ──────────────────────────────────────────────────

def test_saving_a_trip_never_takes_in_another_accounts_activity(bobs_ride):
    """The backstop under every save: an activity reference the trip did not
    already hold must be to one of the saver's own activities."""
    from src.models.activity import Activity
    from src.models.project import ProjectItem

    client, engine, ids, act_as = bobs_ride
    _create_trip(client, "Mine")
    repo = project_shared_mod._repo
    with Session(engine) as sess:
        project = repo.get_project(sess, ids["alice"], "Mine")
        project.activities.append(Activity.from_strava_api(_activity(_VICTIM_ACT, name="X")))
        project.items.append(ProjectItem(item_type="activity", activity_id=_VICTIM_ACT))
        repo.save_project(sess, ids["alice"], project)

    _assert_bobs_row_untouched(engine, ids)
    assert _VICTIM_ACT not in _trip_activity_ids(engine, ids["alice"], "Mine")


def test_background_enrichment_never_writes_another_accounts_row(bobs_ride):
    client, engine, ids, act_as = bobs_ride

    with Session(engine) as sess:
        project_shared_mod._repo.update_activity_enrichment(
            sess, _VICTIM_ACT, "??", None, owner_id=ids["alice"])

    _assert_bobs_row_untouched(engine, ids)


# ── Finding references made before this was enforced ────────────────────────

def test_the_audit_lists_trips_holding_activities_of_accounts_outside_them(env):
    from scripts.audit_activity_ownership import find_foreign_activity_refs

    client, engine, ids, act_as = env
    with Session(engine) as sess:
        for aid, who in ((1, "alice"), (2, "bob"), (3, "carol")):
            sess.add(DBActivity(id=aid, user_info_id=ids[who], name=f"a{aid}"))
        trip = DBProject(user_info_id=ids["alice"], name="Trip")
        sess.add(trip)
        sess.commit()
        from models.project_db import DBProjectMember
        sess.add(DBProjectMember(project_id=trip.id, user_info_id=ids["carol"],
                                 role="editor", invited_by=ids["alice"]))
        for pos, aid in enumerate((1, 2, 3)):
            sess.add(DBProjectItem(project_id=trip.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()

        trip_id = trip.id
        refs = find_foreign_activity_refs(sess)

    # Alice's own and her member Carol's are fine; Bob's is reported.
    assert [(r["project_id"], r["activity_id"], r["activity_owner_id"]) for r in refs] == [
        (trip_id, 2, ids["bob"])]


# ── Activity ids are plain integers ─────────────────────────────────────────

@pytest.mark.parametrize("given", ["9001", 9001.0, " 9001"])
def test_an_id_that_is_not_a_plain_integer_is_not_added(bobs_ride, given):
    client, engine, ids, act_as = bobs_ride
    _create_trip(client, "Mine")
    act = _activity(_VICTIM_ACT, name="Renamed", polyline="??")
    act["id"] = given

    r = _add(client, "Mine", [act])

    assert r.status_code == 200, r.text
    assert r.json()["added"] == 0
    _assert_bobs_row_untouched(engine, ids)
    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()
    _assert_not_exposed(client, "Mine")


def test_a_boolean_id_is_not_added(env):
    client, engine, ids, act_as = env
    act_as("bob")
    _create_trip(client, "Bob trip")
    assert _add(client, "Bob trip", [_activity(1)]).json()["added"] == 1
    act_as("alice")
    _create_trip(client, "Mine")
    act = _activity(1, name="Renamed")
    act["id"] = True

    r = _add(client, "Mine", [act])

    assert r.status_code == 200, r.text
    assert r.json()["added"] == 0
    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()
    assert _row(engine, 1).name == _NAME


@pytest.mark.parametrize("given", ["9001", 9001.0, True])
def test_an_import_item_whose_activity_id_is_not_a_plain_integer_is_refused(bobs_ride, given):
    client, engine, ids, act_as = bobs_ride
    doc = {"version": 1, "name": "x",
           "items": [{"item_type": "activity", "activity_id": given}],
           "activities": []}

    r = client.post("/api/projects/import", files={
        "file": (f"Mine{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 400, r.text
    with Session(engine) as sess:
        assert sess.exec(select(DBProject).where(DBProject.name == "Mine")).first() is None


@pytest.mark.parametrize("given", ["9001", 9001.0, True])
def test_an_import_activity_whose_id_is_not_a_plain_integer_is_refused(bobs_ride, given):
    client, engine, ids, act_as = bobs_ride
    act = _activity(_VICTIM_ACT, name="Renamed")
    act["id"] = given
    doc = {"version": 1, "name": "x", "items": [], "activities": [act]}

    r = client.post("/api/projects/import", files={
        "file": (f"Mine{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 400, r.text
    _assert_bobs_row_untouched(engine, ids)


def test_the_repo_filters_do_not_trust_the_type_of_an_id(bobs_ride):
    """Defence in depth under the parsers: a non-integer id is never the
    caller's."""
    from src.models.activity import Activity
    from src.models.project import ProjectItem

    client, engine, ids, act_as = bobs_ride
    _create_trip(client, "Mine")
    repo = project_shared_mod._repo
    stray = Activity.from_strava_api(_activity(5, name="X"))
    stray.id = str(_VICTIM_ACT)
    with Session(engine) as sess:
        assert repo.own_activities_only(sess, ids["alice"], [stray]) == []
        project = repo.get_project(sess, ids["alice"], "Mine")
        project.items.append(ProjectItem(item_type="activity", activity_id=str(_VICTIM_ACT)))
        repo.save_project(sess, ids["alice"], project)

    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()


# ── References to an activity nobody holds yet ──────────────────────────────

def test_an_import_item_naming_an_activity_nobody_holds_is_left_out(env):
    """Such an item would start pointing at whoever later creates that id."""
    client, engine, ids, act_as = env
    doc = {"version": 1, "name": "x",
           "items": [{"item_type": "activity", "activity_id": 9005}],
           "activities": []}

    r = client.post("/api/projects/import", files={
        "file": (f"Mine{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 201, r.text
    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()

    act_as("bob")
    _create_trip(client, "Bob trip")
    assert _add(client, "Bob trip", [_activity(9005)]).json()["added"] == 1
    act_as("alice")
    r = client.get("/api/projects/Mine")
    assert r.status_code == 200
    assert _NAME not in r.text
    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()


def test_saving_a_trip_never_takes_in_an_activity_nobody_holds_and_it_does_not_carry(env):
    from src.models.project import ProjectItem

    client, engine, ids, act_as = env
    _create_trip(client, "Mine")
    repo = project_shared_mod._repo
    with Session(engine) as sess:
        project = repo.get_project(sess, ids["alice"], "Mine")
        project.items.append(ProjectItem(item_type="activity", activity_id=9006))
        repo.save_project(sess, ids["alice"], project)

    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()


# ── Geometry writes ─────────────────────────────────────────────────────────

def _plant(engine, owner_id: int, trip: str, activity_id: int) -> None:
    """A trip item referencing *activity_id*, as data written before the
    ownership checks existed could hold."""
    with Session(engine) as sess:
        pid = sess.exec(select(DBProject.id).where(
            DBProject.user_info_id == owner_id, DBProject.name == trip)).one()
        sess.add(DBProjectItem(project_id=pid, position=99, item_type="activity",
                               activity_id=activity_id))
        sess.commit()


def _edit(client, trip: str, aid: int, owner_q: str = ""):
    return client.put(f"/api/projects/{trip}/activities/{aid}/track{owner_q}", json={
        "points": [{"lat": 48.0, "lng": 2.0}, {"lat": 48.2, "lng": 2.2},
                   {"lat": 48.4, "lng": 2.4}, {"lat": 48.6, "lng": 2.6}]})


def test_geometry_writes_refuse_an_activity_of_an_account_outside_the_trip(bobs_ride):
    client, engine, ids, act_as = bobs_ride
    with Session(engine) as sess:
        sess.add(DBActivity(id=-55, user_info_id=ids["bob"], name="Bob local",
                            summary_polyline=_POLYLINE))
        sess.commit()
    _create_trip(client, "Mine")
    _plant(engine, ids["alice"], "Mine", _VICTIM_ACT)
    _plant(engine, ids["alice"], "Mine", -55)
    base = f"/api/projects/Mine/activities/{_VICTIM_ACT}"

    assert _edit(client, "Mine", _VICTIM_ACT).status_code == 404
    assert client.post(f"{base}/reset").status_code == 404
    assert client.post(f"{base}/split", json={"split_index": 1}).status_code == 404
    assert client.delete("/api/projects/Mine/activities/-55/local").status_code == 404

    _assert_bobs_row_untouched(engine, ids)
    assert _row(engine, -55) is not None


def test_geometry_writes_follow_membership(env):
    client, engine, ids, act_as = env
    _create_trip(client, "Japan")
    _join(client, act_as, "alice", "carol", "Japan")
    owner_q = f"?owner={ids['alice']}"
    act_as("carol")
    assert _add(client, "Japan", [_activity(7001)], owner_q).json()["added"] == 1

    # The owner may edit a current member's activity...
    act_as("alice")
    assert _edit(client, "Japan", 7001).status_code == 200

    # ...and a split tail belongs to whoever owns what was split.
    r = client.post("/api/projects/Japan/activities/7001/split", json={"split_index": 1})
    assert r.status_code == 200, r.text
    tails = _trip_activity_ids(engine, ids["alice"], "Japan") - {7001}
    assert len(tails) == 1
    assert _row(engine, tails.pop()).user_info_id == ids["carol"]

    # Once the member has left, their activity stays in the trip but is no
    # longer the trip's to rewrite.
    act_as("carol")
    assert client.delete(
        f"/api/projects/Japan/members/{ids['carol']}{owner_q}").status_code == 204
    act_as("alice")
    assert _edit(client, "Japan", 7001).status_code == 404


def test_the_audit_lists_items_whose_activity_does_not_exist(env):
    from scripts.audit_activity_ownership import find_dangling_activity_refs

    client, engine, ids, act_as = env
    _create_trip(client, "Mine")
    _plant(engine, ids["alice"], "Mine", 4242)

    with Session(engine) as sess:
        refs = find_dangling_activity_refs(sess)

    assert [(r["project_name"], r["activity_id"]) for r in refs] == [("Mine", 4242)]


# ── Removing an item ────────────────────────────────────────────────────────

def test_removing_an_item_leaves_a_local_activity_of_an_account_outside_the_trip(env):
    client, engine, ids, act_as = env
    with Session(engine) as sess:
        sess.add(DBActivity(id=-55, user_info_id=ids["bob"], name="Bob local",
                            summary_polyline=_POLYLINE))
        sess.commit()
    _create_trip(client, "Mine")
    _plant(engine, ids["alice"], "Mine", -55)

    r = client.delete("/api/projects/Mine/items/0")

    assert r.status_code == 204, r.text
    assert _trip_activity_ids(engine, ids["alice"], "Mine") == set()
    assert _row(engine, -55) is not None


def test_removing_an_item_still_deletes_the_trips_own_local_activity(env):
    client, engine, ids, act_as = env
    _create_trip(client, "Japan")
    _join(client, act_as, "alice", "carol", "Japan")
    with Session(engine) as sess:
        for aid, who in ((-56, "alice"), (-57, "carol")):
            sess.add(DBActivity(id=aid, user_info_id=ids[who], name=f"local {aid}"))
        sess.commit()
    act_as("alice")
    _plant(engine, ids["alice"], "Japan", -56)
    _plant(engine, ids["alice"], "Japan", -57)

    assert client.delete("/api/projects/Japan/items/0").status_code == 204
    assert client.delete("/api/projects/Japan/items/0").status_code == 204

    assert _row(engine, -56) is None
    assert _row(engine, -57) is None


# ── Ids beyond a 64-bit integer ─────────────────────────────────────────────

_TOO_BIG = 2 ** 63
_TOO_SMALL = -(2 ** 63) - 1


@pytest.mark.parametrize("aid", [_TOO_BIG, _TOO_SMALL])
def test_activity_routes_answer_an_out_of_range_id_without_a_server_error(env, aid):
    client, engine, ids, act_as = env
    _create_trip(client, "Mine")
    base = f"/api/projects/Mine/activities/{aid}"
    calls = [
        ("get", f"{base}/track", None),
        ("put", f"{base}/track", {"points": [{"lat": 48.0, "lng": 2.0},
                                             {"lat": 48.1, "lng": 2.1}]}),
        ("post", f"{base}/reset", None),
        ("post", f"{base}/split", {"split_index": 1}),
        ("post", f"{base}/refresh", None),
        ("delete", f"{base}/local", None),
        ("put", f"/api/activities/{aid}", {"name": "x"}),
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code in (404, 422), (method, path, r.status_code, r.text)


def test_adding_an_out_of_range_id_is_skipped(env):
    client, engine, ids, act_as = env
    _create_trip(client, "Mine")

    r = _add(client, "Mine", [_activity(_TOO_BIG)])

    assert r.status_code == 200, r.text
    assert r.json()["added"] == 0


@pytest.mark.parametrize("where", ["item", "activity"])
def test_an_import_with_an_out_of_range_id_is_refused(env, where):
    client, engine, ids, act_as = env
    doc = {"version": 1, "name": "x", "items": [], "activities": []}
    if where == "item":
        doc["items"] = [{"item_type": "activity", "activity_id": _TOO_BIG}]
    else:
        doc["activities"] = [_activity(_TOO_BIG)]

    r = client.post("/api/projects/import", files={
        "file": (f"Mine{ProjectIO.EXTENSION}", json.dumps(doc).encode(), "application/json")})

    assert r.status_code == 400, r.text
