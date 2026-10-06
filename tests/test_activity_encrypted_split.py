"""``POST …/activities/{id}/split/encrypted`` (E2EE remnants U12, decision 7).

The device cuts an encrypted track and sends both pieces encrypted, with their
figures, and the tail's encrypted name. The server keeps ``split_activity``'s
bookkeeping: a new local tail inserted right after the head, the family links,
the tail starting where the head ends (head start + the head's elapsed time),
the head's snapshot on its first edit, the tail's snapshot being its own
values, and the family renaming that an enveloped root skips.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.activities as activities_mod
import models.db as db_module
from api.activities import router as activities_router
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo

_URL = "/api/projects/Trip/activities/{aid}/split/encrypted"
_START = "2026-05-01T08:00:00Z"
_START_LOCAL = "2026-05-01T10:00:00Z"


def _env(seed: int) -> str:
    """An envelope as ``EncryptedField.encode`` writes one (see
    test_activity_encrypted_edit.py)."""
    wrapped = b"\xfb\xff\xbf" * 24
    ciphertext = bytes([seed % 256]) * 41
    return (f"v1.{base64.b64encode(wrapped).decode()}"
            f".{base64.b64encode(ciphertext).decode()}")


_STORED = dict(
    summary_polyline=_env(1), elevation_profile_json=_env(2),
    elevation_profile_low_res_json=_env(2), start_latlng_json=_env(3),
    end_latlng_json=_env(4), distance=4000.0, moving_time=1000, elapsed_time=1200,
    average_speed=4.0, total_elevation_gain=60.0, elev_high=140.0, elev_low=100.0,
)
_HEAD_NAME = _env(5)
_TAIL_NAME = _env(6)
_SCALARS = ("distance", "moving_time", "elapsed_time", "average_speed",
            "elev_high", "elev_low", "start_latlng_json", "end_latlng_json")


def _piece(seed: int, **over):
    piece = dict(
        summary_polyline=_env(seed), elevation_profile_json=_env(seed + 1),
        start_latlng_json=_env(seed + 2), end_latlng_json=_env(seed + 3),
        distance=1000.0 + seed, moving_time=300 + seed, elapsed_time=400 + seed,
        average_speed=3.0 + seed / 100, total_elevation_gain=10.0 + seed,
        elev_high=130.0 + seed, elev_low=100.0 + seed,
    )
    piece.update(over)
    return piece


@pytest.fixture
def env(monkeypatch):
    """Trip "Trip" (lock 7) holds encrypted activity 111, then plaintext 222."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="Trip", lock_version=7)
        sess.add(proj); sess.commit(); sess.refresh(proj)
        sess.add(DBActivity(id=111, user_info_id=u.id, name=_HEAD_NAME, type="Ride",
                            start_date=_START, start_date_local=_START_LOCAL,
                            timezone="Europe/Paris", source="gpx", **_STORED))
        sess.add(DBActivity(id=222, user_info_id=u.id, name="Plain", type="Ride",
                            summary_polyline="_p~iF~ps|U_ulLnnqC", distance=10.0))
        for pos, aid in enumerate((111, 222)):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()
        uid = u.id

    calls = []
    monkeypatch.setattr(activities_mod, "bust_geo_cache",
                        lambda owner, name: calls.append(("geo", owner, name)))
    monkeypatch.setattr(activities_mod, "queue_stats_refresh",
                        lambda bt, owner, name: calls.append(("stats", owner, name)))
    monkeypatch.setattr(activities_mod, "queue_share_tiles_refresh",
                        lambda bt, owner, name: calls.append(("tiles", owner, name)))

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(activities_router)
    return dict(client=TestClient(app), engine=engine, uid=uid, calls=calls)


def _row(engine, aid):
    with Session(engine) as sess:
        a = sess.get(DBActivity, aid)
        return None if a is None else {c: getattr(a, c) for c in DBActivity.model_fields}


def _items(engine):
    with Session(engine) as sess:
        return [it.activity_id for it in sess.exec(
            select(DBProjectItem).order_by(DBProjectItem.position)).all()]


def _lock(engine):
    with Session(engine) as sess:
        return sess.exec(select(DBProject)).first().lock_version


def _activity_count(engine):
    with Session(engine) as sess:
        return len(sess.exec(select(DBActivity)).all())


def _split(env, aid=111, **over):
    body = dict(head=_piece(20), tail=_piece(40), tail_name=_TAIL_NAME, lock_version=7)
    body.update(over)
    return env["client"].post(_URL.format(aid=aid), json=body)


def _shifted(iso: str, seconds: int) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return dt.isoformat().replace("+00:00", "Z")


def test_split_creates_the_tail_after_the_head_with_the_sent_values(env):
    head, tail = _piece(20), _piece(40)
    resp = _split(env, head=head, tail=tail)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tail_id = body["tail_id"]
    assert body["id"] == 111 and body["lock_version"] == 8
    assert tail_id < 0

    assert _items(env["engine"]) == [111, tail_id, 222]
    assert _lock(env["engine"]) == 8

    h, t = _row(env["engine"], 111), _row(env["engine"], tail_id)
    for field, value in head.items():
        assert h[field] == value, field
    for field, value in tail.items():
        assert t[field] == value, field
    assert h["elevation_profile_low_res_json"] == head["elevation_profile_json"]
    assert t["elevation_profile_low_res_json"] == tail["elevation_profile_json"]
    assert h["is_edited"] is True and t["is_edited"] is True

    # The family: links, owner, origin and metadata carried over.
    assert t["split_root_id"] == 111 and t["split_parent_id"] == 111
    assert t["user_info_id"] == env["uid"]
    assert t["manual"] is True
    assert t["source"] == "gpx" and t["timezone"] == "Europe/Paris"

    # The tail starts where the head ends: head start + the head's elapsed time.
    assert t["start_date"] == _shifted(_START, head["elapsed_time"])
    assert t["start_date_local"] == _shifted(_START_LOCAL, head["elapsed_time"])
    assert h["start_date"] == _START

    # Names: the sent envelope on the tail, the head's untouched, no plaintext base.
    assert t["name"] == _TAIL_NAME
    assert h["name"] == _HEAD_NAME
    assert h["split_base_name"] is None

    owner = env["uid"]
    assert sorted(env["calls"]) == [("geo", owner, "Trip"), ("stats", owner, "Trip"),
                                    ("tiles", owner, "Trip")]


def test_the_head_snapshots_its_pre_split_track_and_the_tail_its_own_values(env):
    resp = _split(env)
    assert resp.status_code == 200, resp.text
    tail_id = resp.json()["tail_id"]
    h, t = _row(env["engine"], 111), _row(env["engine"], tail_id)

    assert h["original_polyline"] == _STORED["summary_polyline"]
    assert h["original_elevation_profile_json"] == _STORED["elevation_profile_json"]
    assert h["original_total_elevation_gain"] == _STORED["total_elevation_gain"]
    for field in _SCALARS:
        assert h[f"original_{field}"] == _STORED[field], field

    assert t["original_polyline"] == t["summary_polyline"]
    assert t["original_elevation_profile_json"] == t["elevation_profile_json"]
    assert t["original_total_elevation_gain"] == t["total_elevation_gain"]
    for field in _SCALARS:
        assert t[f"original_{field}"] == t[field], field


def test_resetting_the_head_undoes_the_split_and_restores_it_exactly(env):
    tail_id = _split(env).json()["tail_id"]
    resp = env["client"].post("/api/projects/Trip/activities/111/reset",
                              json={"lock_version": 8})
    assert resp.status_code == 200, resp.text
    assert _row(env["engine"], tail_id) is None
    assert _items(env["engine"]) == [111, 222]
    h = _row(env["engine"], 111)
    for field, value in _STORED.items():
        assert h[field] == value, field
    assert h["is_edited"] is False


def test_stale_lock_version_is_refused_and_creates_nothing(env):
    before = (_row(env["engine"], 111), _items(env["engine"]), _activity_count(env["engine"]))
    resp = _split(env, lock_version=6)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "stale_write"
    assert (_row(env["engine"], 111), _items(env["engine"]),
            _activity_count(env["engine"])) == before
    assert _lock(env["engine"]) == 7


def test_lock_version_is_required(env):
    body = dict(head=_piece(20), tail=_piece(40), tail_name=_TAIL_NAME)
    assert env["client"].post(_URL.format(aid=111), json=body).status_code == 422


def test_plaintext_row_is_refused_as_not_encrypted(env):
    before = (_row(env["engine"], 222), _activity_count(env["engine"]))
    resp = _split(env, aid=222)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "not_encrypted"
    assert (_row(env["engine"], 222), _activity_count(env["engine"])) == before
    assert _lock(env["engine"]) == 7


@pytest.mark.parametrize("over", [
    dict(tail_name="v1.2.3"),
    dict(tail_name="Ride (2/2)"),
    dict(head=_piece(20, summary_polyline="v1.2.3")),
    dict(tail=_piece(40, end_latlng_json="[48.0, 2.0]")),
    dict(tail=_piece(40, distance=-5.0)),
    dict(head=_piece(20, elev_high=None)),
], ids=["tail-name-v1.2.3", "tail-name-plain", "head-polyline", "tail-endpoint",
        "tail-distance", "head-partial-elevation"])
def test_a_malformed_piece_or_name_is_422_and_creates_nothing(env, over):
    before = (_row(env["engine"], 111), _activity_count(env["engine"]))
    resp = _split(env, **over)
    assert resp.status_code == 422, resp.text
    assert (_row(env["engine"], 111), _activity_count(env["engine"])) == before


def test_a_plaintext_root_still_renames_the_family(env):
    """The rename belongs to split_activity's bookkeeping, kept as it is: it
    is skipped only for an enveloped root. A head cut out of a plaintext
    root is renamed from the name the server already holds in plaintext."""
    with Session(env["engine"]) as sess:
        head = sess.get(DBActivity, 111)
        head.name = "Alps ride"
        sess.add(head); sess.commit()
    resp = _split(env)
    assert resp.status_code == 200, resp.text
    tail_id = resp.json()["tail_id"]
    assert _row(env["engine"], 111)["name"] == "Alps ride (1/2)"
    assert _row(env["engine"], tail_id)["name"] == "Alps ride (2/2)"
