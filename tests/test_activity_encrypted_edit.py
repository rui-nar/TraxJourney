"""``PUT …/activities/{id}/track/encrypted`` (E2EE remnants U12, decision 7).

The track of an encrypted activity is edited on the device. The route stores
the geometry envelopes and figures the device sends, exactly, with the same
bookkeeping as ``PUT …/track``: the first edit snapshots the previous track
and figures, the trip's lock version advances (compare-and-swap, required),
and the stats, share tiles and geo cache are refreshed.

Only well-formed envelopes are taken — the strict shape of
``EncryptedField.isWellFormed``, standard base64 with padding (R5-2) — and
only figures inside the bounds every stored figure obeys. A track with no
elevations sends its profile and both elevation extremes null, together
(R5-4).
"""
from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.activities as activities_mod
import models.db as db_module
from api.activities import router as activities_router
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject, DBProjectItem, DBProjectMember
from models.user import UserInfo

_URL = "/api/projects/Trip/activities/{aid}/track/encrypted"


def _env(seed: int) -> str:
    """An envelope shaped as ``EncryptedField.encode`` writes one: a 72-byte
    wrapped DEK and a 41-byte ciphertext, standard base64 with padding. The
    DEK bytes encode to "+/+/…" and the ciphertext ends in "=", so every
    envelope here carries the three characters base64url would not."""
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


def _piece(**over):
    piece = dict(
        summary_polyline=_env(11), elevation_profile_json=_env(12),
        start_latlng_json=_env(13), end_latlng_json=_env(14),
        distance=2500.5, moving_time=600, elapsed_time=700, average_speed=4.1675,
        total_elevation_gain=33.25, elev_high=135.5, elev_low=101.0,
    )
    piece.update(over)
    return piece


@pytest.fixture
def env(monkeypatch):
    """Owner's trip "Trip" (lock 7) holds encrypted activity 111 and
    plaintext activity 222. Ed is an editor, Vi a viewer."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("owner", "ed", "vi")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}
        proj = DBProject(user_info_id=ids["owner"], name="Trip", lock_version=7)
        sess.add(proj); sess.commit(); sess.refresh(proj)
        for name, role in (("ed", "editor"), ("vi", "viewer")):
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=ids[name],
                                     role=role, invited_by=ids["owner"]))
        sess.add(DBActivity(id=111, user_info_id=ids["owner"], name=_env(5),
                            type="Ride", **_STORED))
        sess.add(DBActivity(id=222, user_info_id=ids["owner"], name="Plain", type="Ride",
                            summary_polyline="_p~iF~ps|U_ulLnnqC", distance=10.0))
        for pos, aid in enumerate((111, 222)):
            sess.add(DBProjectItem(project_id=proj.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()

    calls = []
    monkeypatch.setattr(activities_mod, "bust_geo_cache",
                        lambda owner, name: calls.append(("geo", owner, name)))
    monkeypatch.setattr(activities_mod, "queue_stats_refresh",
                        lambda bt, owner, name: calls.append(("stats", owner, name)))
    monkeypatch.setattr(activities_mod, "queue_share_tiles_refresh",
                        lambda bt, owner, name: calls.append(("tiles", owner, name)))

    caller = {"id": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(caller["id"])}
    app.include_router(activities_router)
    return dict(client=TestClient(app), engine=engine, ids=ids, caller=caller, calls=calls)


def _row(engine, aid=111):
    with Session(engine) as sess:
        a = sess.get(DBActivity, aid)
        return {c: getattr(a, c) for c in DBActivity.model_fields}


def _lock(engine):
    with Session(engine) as sess:
        return sess.exec(select(DBProject)).first().lock_version


def _put(env, aid=111, **body):
    url = _URL.format(aid=aid)
    if env["caller"]["id"] != env["ids"]["owner"]:
        url += f"?owner={env['ids']['owner']}"
    return env["client"].put(url, json=body)


def test_test_envelopes_carry_standard_base64_characters():
    assert all(ch in _env(11) for ch in "+/=")


def test_edit_stores_exactly_what_was_sent_and_snapshots_the_previous_track(env):
    piece = _piece()
    resp = _put(env, **piece, lock_version=7)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": 111, "lock_version": 8}

    row = _row(env["engine"])
    for field, value in piece.items():
        assert row[field] == value, field
    assert row["elevation_profile_low_res_json"] == piece["elevation_profile_json"]
    assert row["is_edited"] is True
    assert row["name"] == _env(5)
    assert row["original_polyline"] == _STORED["summary_polyline"]
    assert row["original_elevation_profile_json"] == _STORED["elevation_profile_json"]
    assert row["original_total_elevation_gain"] == _STORED["total_elevation_gain"]
    for field in ("distance", "moving_time", "elapsed_time", "average_speed",
                  "elev_high", "elev_low", "start_latlng_json", "end_latlng_json"):
        assert row[f"original_{field}"] == _STORED[field], field
    assert _lock(env["engine"]) == 8
    owner = env["ids"]["owner"]
    assert sorted(env["calls"]) == [("geo", owner, "Trip"), ("stats", owner, "Trip"),
                                    ("tiles", owner, "Trip")]


def test_a_second_edit_keeps_the_first_snapshot(env):
    assert _put(env, **_piece(), lock_version=7).status_code == 200
    second = _piece(summary_polyline=_env(21), distance=1000.0)
    assert _put(env, **second, lock_version=8).status_code == 200
    row = _row(env["engine"])
    assert row["summary_polyline"] == _env(21)
    assert row["distance"] == 1000.0
    assert row["original_polyline"] == _STORED["summary_polyline"]
    assert row["original_distance"] == _STORED["distance"]


def test_an_editor_member_may_edit(env):
    env["caller"]["id"] = env["ids"]["ed"]
    assert _put(env, **_piece(), lock_version=7).status_code == 200


def test_a_viewer_may_not(env):
    env["caller"]["id"] = env["ids"]["vi"]
    before = _row(env["engine"])
    assert _put(env, **_piece(), lock_version=7).status_code == 403
    assert _row(env["engine"]) == before


def test_an_activity_the_trip_does_not_hold_is_404(env):
    assert _put(env, aid=999, **_piece(), lock_version=7).status_code == 404


def test_stale_lock_version_is_refused_and_writes_nothing(env):
    before = _row(env["engine"])
    resp = _put(env, **_piece(), lock_version=6)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "stale_write"
    assert _row(env["engine"]) == before
    assert _lock(env["engine"]) == 7
    assert env["calls"] == []


def test_lock_version_is_required(env):
    assert _put(env, **_piece()).status_code == 422
    assert _lock(env["engine"]) == 7


def test_plaintext_row_is_refused_as_not_encrypted(env):
    before = _row(env["engine"], 222)
    resp = _put(env, aid=222, **_piece(), lock_version=7)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "not_encrypted"
    assert _row(env["engine"], 222) == before
    assert _lock(env["engine"]) == 7


_GOOD = _env(11)
_WRAPPED, _CIPHER = _GOOD.split(".")[1:]


@pytest.mark.parametrize("bad", [
    "v1.2.3",
    "plaintext polyline",
    f"v2.{_WRAPPED}.{_CIPHER}",
    f"v1.{_WRAPPED.replace('+', '-').replace('/', '_')}.{_CIPHER}",   # base64url
    f"v1.{_WRAPPED}.{_CIPHER.rstrip('=')}",                           # no padding
    f"v1.{base64.b64encode(bytes(71)).decode()}.{_CIPHER}",           # short wrapped DEK
    f"v1.{_WRAPPED}.{base64.b64encode(bytes(39)).decode()}",          # short ciphertext
    f"v1.{_WRAPPED}.",
    f"v1.{_WRAPPED}.{_CIPHER}.x",
], ids=["v1.2.3", "plain", "v2", "base64url", "unpadded", "short-dek",
        "short-ciphertext", "empty-ciphertext", "four-parts"])
@pytest.mark.parametrize("field", ["summary_polyline", "elevation_profile_json",
                                   "start_latlng_json", "end_latlng_json"])
def test_a_malformed_envelope_is_422_and_writes_nothing(env, field, bad):
    before = _row(env["engine"])
    resp = _put(env, **_piece(**{field: bad}), lock_version=7)
    assert resp.status_code == 422, resp.text
    assert _row(env["engine"]) == before
    assert _lock(env["engine"]) == 7


@pytest.mark.parametrize("field,value", [
    ("distance", -1.0),
    ("distance", 1e8 + 1),
    ("moving_time", -1),
    ("moving_time", 1.5),
    ("elapsed_time", 1e9 + 1),
    ("average_speed", -0.1),
    ("total_elevation_gain", -1.0),
    ("total_elevation_gain", 1e7 + 1),
    ("elev_high", 20_001.0),
    ("elev_low", -20_001.0),
    ("elev_low", 136.0),          # above elev_high
    ("distance", "NaN"),
    ("distance", "Infinity"),
    ("total_elevation_gain", "-Infinity"),
])
def test_a_figure_out_of_bounds_is_422_and_writes_nothing(env, field, value):
    import json

    before = _row(env["engine"])
    body = dict(_piece(), lock_version=7)
    # NaN and Infinity as JSON tokens, the way a careless encoder writes them.
    raw = json.dumps(body).replace(f'"{field}": {json.dumps(body[field])}',
                                   f'"{field}": {value}')
    assert f'"{field}": {value}' in raw
    resp = env["client"].put(_URL.format(aid=111), content=raw,
                             headers={"content-type": "application/json"})
    assert resp.status_code == 422, resp.text
    assert _row(env["engine"]) == before
    assert _lock(env["engine"]) == 7


def test_a_negative_elevation_is_a_reading_not_an_error(env):
    """Below sea level is real (the Dead Sea): only the non-elevation figures
    must be non-negative."""
    resp = _put(env, **_piece(elev_high=-300.0, elev_low=-420.0), lock_version=7)
    assert resp.status_code == 200, resp.text


def test_a_track_with_no_elevations_is_stored_with_null_profile(env):
    resp = _put(env, **_piece(elevation_profile_json=None, elev_high=None, elev_low=None),
                lock_version=7)
    assert resp.status_code == 200, resp.text
    row = _row(env["engine"])
    assert row["elevation_profile_json"] is None
    assert row["elevation_profile_low_res_json"] is None
    assert row["elev_high"] is None and row["elev_low"] is None


@pytest.mark.parametrize("nulls", [
    dict(elevation_profile_json=None),
    dict(elev_high=None),
    dict(elev_low=None),
    dict(elev_high=None, elev_low=None),
    dict(elevation_profile_json=None, elev_low=None),
])
def test_partly_null_elevations_are_422(env, nulls):
    before = _row(env["engine"])
    resp = _put(env, **_piece(**nulls), lock_version=7)
    assert resp.status_code == 422, resp.text
    assert _row(env["engine"]) == before
