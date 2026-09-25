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
        yield TestClient(router.app), engine, ids, act_as
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
    assert "Bob GPX" not in client.get("/api/projects/Mine").text


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
