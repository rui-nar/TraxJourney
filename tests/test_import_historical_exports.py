"""Every trip file the app ever wrote still imports (issue #462).

The import now refuses a file holding a value the format does not allow
(test_import_value_schema.py). The format is what the writers emit, and they
changed over time: fields were added, some are written as null, activities
carry whatever Strava sent. A file exported by any past version must still be
accepted, and the trip it makes must load.

``fixtures/traxj_history/`` holds one file per shape a past writer produced,
named for the date and commit of the writer it mirrors, from the desktop
app's ``ProjectIO.save`` (.gettracks) to today's compact export. Between them
they pin what later writers stopped or started emitting:

* keys missing from files written before they existed (no elevation profile,
  segment date, trip_start, route fields, public_id, people, groups...);
* Strava's ``[]`` start and end for an indoor ride (desktop era), null once
  the database stored them; a float ``max_heartrate``; integer elevations;
* app-made negative activity ids, from desktop GPX, split pieces and GPX
  uploads, with naive, fractional and offset start dates;
* an all-null day written as ``{}``, per-day counters as a map (before
  1f40c355) then a list, a day's explicit ``[]`` tags;
* a rail route of ``"[]"``, ferry and bus routes, a failed resolve;
* a phantom segment: an item whose link was lost, exported as a segment with
  an empty id at (0, 0);
* end-to-end encrypted names, descriptions and tracks, with null geometry;
* the ZIP export's own document, whose memories carry ``photo_refs``.
* a GPX upload from before #462, with a ``65535`` and a ``NaN`` reading, an
  infinite gain and a stray 1970 stamp that made it 54 years long, beside a
  Strava activity whose stream held the same sentinel.

The round trips below cover what today's writers emit: the trip export, the
trip file inside the ZIP export, ``ProjectIO.save``, and a trip whose content
the client encrypted end to end.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.project_shared as project_shared_mod
import api.project_transfer as project_transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBMemory
from models.user import UserInfo
from src.project.project_io import ProjectIO
from tests.test_export_compact import _content, _trip

_HISTORY = Path(__file__).parent / "fixtures" / "traxj_history"
_ENVELOPE = "v1.d3JhcHBlZA.Y2lwaGVy"


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, project_transfer_mod, storage_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="A", email="a@e.com")
        sess.add(user)
        sess.commit()
        uid = user.id
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _import(client, name: str, content: bytes):
    return client.post(
        "/api/projects/import",
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _strict_json(text: str):
    """Parse as the client does: Dart's jsonDecode refuses NaN and Infinity."""
    def _refuse(token):
        raise ValueError(f"not JSON: {token}")
    return json.loads(text, parse_constant=_refuse)


def _loads_and_exports(client, name: str) -> bytes:
    """The imported trip opens in the app, and exports again."""
    r = client.get(f"/api/projects/{name}")
    assert r.status_code == 200, r.text
    _strict_json(r.text)
    r = client.get(f"/api/projects/{name}/export-traxj")
    assert r.status_code == 200, r.text
    return r.content


# ── Files past versions wrote ───────────────────────────────────────────────

_FILES = sorted(_HISTORY.glob(f"*{ProjectIO.EXTENSION}"))


def test_there_are_historical_files():
    assert len(_FILES) >= 8


@pytest.mark.parametrize("path", _FILES, ids=[p.stem for p in _FILES])
def test_a_file_a_past_version_wrote_still_imports(env, path):
    client, engine = env
    raw = path.read_bytes()

    r = _import(client, "Old", raw)

    assert r.status_code == 201, r.text
    exported = _loads_and_exports(client, "Old")
    # What it holds came through: every activity and item of the file.
    doc = json.loads(raw)
    back = json.loads(exported)
    assert len(back["activities"]) == len(doc.get("activities", []))
    assert len(back["items"]) == len(doc["items"])
    # And today's export of it imports again.
    assert _import(client, "Again", exported).status_code == 201


# ── What today's writers emit ───────────────────────────────────────────────

def test_the_trip_export_of_every_kind_of_content_imports_back(env):
    client, _ = env
    assert _import(client, "Alps", _trip(2, 100)).status_code == 201
    exported = _loads_and_exports(client, "Alps")
    assert {i["item_type"] for i in json.loads(exported)["items"]} == {
        "activity", "memory", "journal", "encounter", "segment"}

    r = _import(client, "Restored", exported)

    assert r.status_code == 201, r.text
    assert _content(_loads_and_exports(client, "Restored")) == _content(exported)


@pytest.mark.parametrize("source", ["gpx", "gpx-unrounded"])
def test_an_export_of_uploaded_gpx_tracks_imports_back(env, source):
    client, _ = env
    assert _import(client, "Hike", _trip(2, 300, source)).status_code == 201
    exported = _loads_and_exports(client, "Hike")

    assert _import(client, "Restored", exported).status_code == 201


def test_the_trip_file_inside_the_zip_export_imports(env):
    """It carries photo_refs on memories, and leaves out people and day notes."""
    client, _ = env
    assert _import(client, "Alps", _trip(2, 100)).status_code == 201
    r = client.get("/api/projects/Alps/export-zip")
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    (name,) = [n for n in zf.namelist() if n.endswith(ProjectIO.EXTENSION)]
    body = zf.read(name)
    assert any("photo_refs" in (i.get("memory") or {}) for i in json.loads(body)["items"])

    r = _import(client, "Restored", body)

    assert r.status_code == 201, r.text
    _loads_and_exports(client, "Restored")


def test_a_file_projectio_saved_imports(env, tmp_path):
    client, _ = env
    path = tmp_path / f"t{ProjectIO.EXTENSION}"
    ProjectIO.save(ProjectIO.from_bytes(_trip(2, 100)), str(path))

    r = _import(client, "Saved", path.read_bytes())

    assert r.status_code == 201, r.text
    _loads_and_exports(client, "Saved")


def test_an_export_of_an_encrypted_trip_imports_back(env):
    """With end-to-end encryption on, the client stores ciphertext envelopes
    in place of an activity's name, track, start and end, and of a memory's
    and journal entry's text (issue #29)."""
    client, engine = env
    assert _import(client, "Alps", _trip(2, 100)).status_code == 201
    with Session(engine) as sess:
        for act in sess.exec(select(DBActivity)).all():
            act.name = _ENVELOPE
            act.summary_polyline = _ENVELOPE
            act.start_latlng_json = _ENVELOPE
            act.end_latlng_json = _ENVELOPE
            act.elevation_profile_json = _ENVELOPE
            act.elevation_profile_low_res_json = _ENVELOPE
            sess.add(act)
        for mem in sess.exec(select(DBMemory)).all():
            mem.name = _ENVELOPE
            mem.description = _ENVELOPE
            sess.add(mem)
        sess.commit()
    exported = _loads_and_exports(client, "Alps")
    act = json.loads(exported)["activities"][0]
    assert act["start_latlng"] is None and act["start_latlng_enc"] == _ENVELOPE

    r = _import(client, "Restored", exported)

    assert r.status_code == 201, r.text
    _loads_and_exports(client, "Restored")
