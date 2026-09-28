"""An encrypted activity's track endpoints and elevation survive an import (#466).

With end-to-end encryption on, the client stores an activity's start and end
points and its elevation profile (full and low-res) as ciphertext envelopes.
The export writes them under ``start_latlng_enc``, ``end_latlng_enc`` and
``elevation_profile_enc``, since the plain fields cannot hold them. The import
built a new activity row from the plain fields only, so whenever it created
one (another account's copy, or the same account's activity whose row is gone)
the four columns became NULL: the name and track survived, the endpoints and
elevation did not.

Now a new row takes the envelopes into those columns exactly as the client's
encryption migration writes them (the low-res column holds the same envelope
as the full one, encryption_migration.dart). Only a well-formed envelope is
taken: the trip-file schema refuses anything else in those fields. A row
another account owns is never written (#468). An encrypted row made by an
import behaves as one the client encrypted: no prepared geometry, no low-res
line, no plaintext anywhere.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session, delete, select

from api.deps import get_current_user
from models.project_db import (
    DBActivity, DBActivityGeoPrepared, DBProject, DBProjectItem,
)
from models.user import UserInfo
from src.project.project_io import ProjectIO
from tests.test_elevation_non_finite import _upload_gpx, app  # noqa: F401

#: Envelopes as the client writes them: v1.<base64>.<base64>.
ENV = {
    "name": "v1.bmFtZUtleQ==.bmFtZUNpcGhlcg==",
    "track": "v1.dHJhY2tLZXk=.dHJhY2tDaXBoZXI=",
    "start": "v1.c3RhcnRLZXk=.c3RhcnRDaXBoZXI=",
    "end": "v1.ZW5kS2V5.ZW5kQ2lwaGVy",
    "profile": "v1.cHJvZmlsZUtleQ==.cHJvZmlsZUNpcGhlcg==",
}
_ENCRYPTED_COLUMNS = ("start_latlng_json", "end_latlng_json",
                      "elevation_profile_json", "elevation_profile_low_res_json")


def _encrypt(client, activity_id):
    """What EncryptionMigration.run() sends for an activity."""
    r = client.put(f"/api/activities/{activity_id}", json={
        "name": ENV["name"], "summary_polyline": ENV["track"],
        "start_latlng_json": ENV["start"], "end_latlng_json": ENV["end"],
        "elevation_profile_json": ENV["profile"],
        "elevation_profile_low_res_json": ENV["profile"]})
    assert r.status_code == 200, r.text


def _encrypted_trip(client) -> tuple[int, bytes]:
    activity_id = _upload_gpx(client, [100.0, 110.0, 120.0])
    _encrypt(client, activity_id)
    exported = client.get("/api/projects/Trip/export-traxj")
    assert exported.status_code == 200, exported.text
    return activity_id, exported.content


def _import(client, name, content, **params):
    return client.post("/api/projects/import", params=params, files={
        "file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _activity_of(engine, trip: str) -> DBActivity:
    with Session(engine) as sess:
        project = sess.exec(select(DBProject).where(DBProject.name == trip)).one()
        aid = sess.exec(select(DBProjectItem.activity_id).where(
            DBProjectItem.project_id == project.id,
            DBProjectItem.item_type == "activity")).one()
        return sess.get(DBActivity, aid)


def _as_the_client_encrypted_it(row: DBActivity):
    assert row.name == ENV["name"]
    assert row.summary_polyline == ENV["track"]
    assert (row.start_latlng_json, row.end_latlng_json) == (ENV["start"], ENV["end"])
    assert row.elevation_profile_json == ENV["profile"]
    assert row.elevation_profile_low_res_json == ENV["profile"]


def _forget_the_row(engine, activity_id):
    """The same account, where the activity's row no longer exists: another
    server, or a trip deleted with its activities."""
    with Session(engine) as sess:
        sess.execute(delete(DBProjectItem).where(DBProjectItem.activity_id == activity_id))
        sess.execute(delete(DBActivityGeoPrepared).where(
            DBActivityGeoPrepared.activity_id == activity_id))
        sess.execute(delete(DBActivity).where(DBActivity.id == activity_id))
        sess.commit()


def _as_another_account(engine):
    import api.router as router
    with Session(engine) as sess:
        other = UserInfo(display_name="B", email="b@e.com")
        sess.add(other)
        sess.commit()
        other_id = other.id
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(other_id)}
    return other_id


# ── A new row takes the envelopes ───────────────────────────────────────────

def test_another_account_s_copy_keeps_the_encrypted_endpoints_and_elevation(app):  # noqa: F811
    client, engine = app
    activity_id, exported = _encrypted_trip(client)
    other_id = _as_another_account(engine)

    r = _import(client, "Theirs", exported)

    assert r.status_code == 201, r.text
    row = _activity_of(engine, "Theirs")
    assert row.id != activity_id and row.user_info_id == other_id
    _as_the_client_encrypted_it(row)
    # The owner's own row is untouched.
    with Session(engine) as sess:
        _as_the_client_encrypted_it(sess.get(DBActivity, activity_id))


@pytest.mark.parametrize("name, params", [
    ("Restored", {}), ("Trip", {"on_conflict": "copy"}), ("Trip", {"on_conflict": "replace"}),
], ids=["new trip", "keep both", "replace"])
def test_the_same_account_whose_row_is_gone_gets_it_back_whole(app, name, params):  # noqa: F811
    client, engine = app
    activity_id, exported = _encrypted_trip(client)
    _forget_the_row(engine, activity_id)

    r = _import(client, name, exported, **params)

    assert r.status_code == 201, r.text
    row = _activity_of(engine, r.json()["name"])
    assert row.id == activity_id
    _as_the_client_encrypted_it(row)


def test_the_trip_reads_back_as_an_encrypted_one(app):  # noqa: F811
    """The client decrypts from the *_enc fields, whichever load it makes;
    the server keeps no plaintext geometry for it."""
    client, engine = app
    activity_id, exported = _encrypted_trip(client)
    _forget_the_row(engine, activity_id)
    assert _import(client, "Back", exported).status_code == 201

    for url in ("/api/projects/Back", "/api/projects/Back/meta"):
        act = client.get(url).json()["activities"][0]
        assert (act["start_latlng"], act["end_latlng"]) == (None, None)
        assert (act["start_latlng_enc"], act["end_latlng_enc"]) == (ENV["start"], ENV["end"])
        assert act["elevation_profile_enc"] == ENV["profile"]
    with Session(engine) as sess:
        assert sess.get(DBActivityGeoPrepared, activity_id) is None
        low_res = json.loads(sess.exec(select(DBProject).where(
            DBProject.name == "Back")).one().low_res_geo_json)
    assert low_res["features"] == []
    assert client.get("/api/projects/Back/stats").status_code == 200


# ── Only an envelope is taken ───────────────────────────────────────────────

@pytest.mark.parametrize("field, value", [
    ("start_latlng_enc", "[45.0, 6.0]"),
    ("end_latlng_enc", "v1.only-two"),
    ("elevation_profile_enc", "v2.a2V5.Y2lwaGVy"),
    ("start_latlng_enc", "v1.not base64!.Y2lwaGVy"),
    ("end_latlng_enc", "v1..Y2lwaGVy"),
    ("elevation_profile_enc", 5),
], ids=["plaintext", "two parts", "other version", "not base64", "empty part", "a number"])
def test_what_is_not_an_envelope_is_refused(app, field, value):  # noqa: F811
    client, _ = app
    _, exported = _encrypted_trip(client)
    doc = json.loads(exported)
    doc["activities"][0][field] = value

    r = _import(client, "Bad", json.dumps(doc).encode())

    assert r.status_code == 400, r.text
    assert f": activities[0].{field} " in r.json()["detail"], r.json()["detail"]


def test_a_plain_endpoint_wins_over_an_envelope(app):  # noqa: F811
    """No writer emits both; if a file does, the readable one is kept."""
    client, engine = app
    activity_id, exported = _encrypted_trip(client)
    _forget_the_row(engine, activity_id)
    doc = json.loads(exported)
    doc["activities"][0]["start_latlng"] = [48.0, 2.0]

    assert _import(client, "Mixed", json.dumps(doc).encode()).status_code == 201

    row = _activity_of(engine, "Mixed")
    assert row.start_latlng_json == "[48.0, 2.0]"
    assert row.end_latlng_json == ENV["end"]
