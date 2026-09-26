"""Tests for E2EE activity encryption (issue #29).

Covers:
  1. The shared is_encrypted_envelope() structural check.
  2. The three CRITICAL sync-stomp guards — a background Strava
     sync/enrichment/force-refresh must never silently overwrite an
     already-encrypted field back to plaintext, while still refreshing
     non-encrypted scalar fields (e.g. kudos_count) normally.
  3. Server-side geo/GPX/share guards — encrypted geometry must never crash a
     json.loads()/polyline decode; the activity is skipped, like "no geometry".
  4. The new narrow PUT /api/activities/{id} field-update endpoint used by the
     client's encryption-enable migration (EncryptionMigration.run()).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import polyline as polyline_lib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.deps import get_current_user
from api.activities import activity_fields_router
from api.geo import _build_full_geo_features, _geo_cache
from api.geo import router as geo_router
from api.project_transfer import router as project_transfer_router
from api.share import _build_features
from models.project_db import DBActivity, DBProject, DBProjectItem
from models.user import UserInfo
from src.models.activity import Activity
from src.models.project import Project, ProjectItem
from src.project.project_repo import ProjectRepo
from src.utils.encryption_check import is_encrypted_envelope

# Envelope-shaped (v1.<b64>.<b64>) strings — the server never decrypts these,
# it only needs the structural v1.x.y shape, so the payload content is
# arbitrary/opaque.
_ENC_NAME = "v1.d2VsY29tZQ==.Y2lwaGVydGV4dA=="
_ENC_POLY = "v1.d2VsY29tZQ==.cG9seWxpbmU="
_ENC_ELEV = "v1.d2VsY29tZQ==.ZWxldmF0aW9u"
_ENC_LOWRES = "v1.d2VsY29tZQ==.bG93cmVz"
_ENC_START = "v1.d2VsY29tZQ==.c3RhcnQ="
_ENC_END = "v1.d2VsY29tZQ==.ZW5k"

_TRACK = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)]


# ---------------------------------------------------------------------------
# 1. Shared helper
# ---------------------------------------------------------------------------

class TestIsEncryptedEnvelope:
    def test_plaintext_is_not_envelope(self):
        assert is_encrypted_envelope("Morning Ride") is False

    def test_none_and_empty_are_not_envelopes(self):
        assert is_encrypted_envelope(None) is False
        assert is_encrypted_envelope("") is False

    def test_encoded_polyline_is_not_mistaken_for_envelope(self):
        # Google-encoded polylines are dot-free ASCII, but guard against any
        # plaintext string that happens to contain two dots.
        assert is_encrypted_envelope("v1.2 notes about the trip") is False

    def test_v1_envelope_is_detected(self):
        assert is_encrypted_envelope(_ENC_NAME) is True


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_activity(id=222, name="Fresh Ride", **overrides) -> Activity:
    base = dict(
        id=id, name=name, type="Ride", distance=5000.0, moving_time=1200,
        elapsed_time=1300, total_elevation_gain=80.0,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        start_date_local=datetime(2026, 1, 1, tzinfo=timezone.utc),
        timezone="UTC", achievement_count=0, kudos_count=99, comment_count=0,
        athlete_count=1, photo_count=0, trainer=False, commute=False, manual=False,
        private=False, flagged=False, average_speed=3.0, max_speed=5.0,
        has_heartrate=False, pr_count=0, total_photo_count=0, has_kudoed=False,
        start_latlng=[48.0, 2.0], end_latlng=[48.0, 2.02],
        summary_polyline=polyline_lib.encode(_TRACK),
        elevation_profile=([0.0, 1.0, 2.0], [10.0, 20.0, 15.0]),
    )
    base.update(overrides)
    return Activity(**base)


@pytest.fixture
def repo_env(monkeypatch):
    """In-memory DB with one user and one activity (id=111) whose name,
    summary_polyline, start/end latlng and elevation profiles are already
    client-side E2EE ciphertext envelopes."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    _geo_cache.clear()

    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="a@b.c")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id

        encrypted = DBActivity(
            id=111, user_info_id=uid, name=_ENC_NAME, type="Ride",
            kudos_count=1,
            summary_polyline=_ENC_POLY,
            elevation_profile_json=_ENC_ELEV,
            elevation_profile_low_res_json=_ENC_LOWRES,
            start_latlng_json=_ENC_START,
            end_latlng_json=_ENC_END,
            start_date="2026-01-01T00:00:00Z",
            start_date_local="2026-01-01T00:00:00Z",
        )
        sess.add(encrypted)
        sess.commit()

    return engine, uid, ProjectRepo()


def _client(engine, uid, *routers) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid), "email": "a@b.c"}
    for r in routers:
        app.include_router(r)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 2. CRITICAL — the three sync-stomp guards
# ---------------------------------------------------------------------------

class TestUpsertActivityGuard:
    """ActivityMixin._upsert_activity — used on every Strava/Polarsteps sync."""

    def test_encrypted_name_survives_resync(self, repo_env):
        engine, uid, repo = repo_env
        fresh = _make_activity(id=111, name="Renamed On Strava", kudos_count=42)
        with Session(engine) as sess:
            repo._upsert_activity(sess, uid, fresh)
            sess.commit()
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            assert row.name == _ENC_NAME
            # Non-encrypted scalar field still updates normally.
            assert row.kudos_count == 42

    def test_summary_polyline_already_safe_without_extra_guard(self, repo_env):
        """summary_polyline only fills in when the existing value is None, and
        an encrypted envelope is never None, so it's naturally protected —
        this pins that behaviour."""
        engine, uid, repo = repo_env
        fresh = _make_activity(id=111, summary_polyline="freshencodedpolyline")
        with Session(engine) as sess:
            repo._upsert_activity(sess, uid, fresh)
            sess.commit()
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            assert row.summary_polyline == _ENC_POLY

    def test_plaintext_activity_still_updates_normally(self, repo_env):
        """Non-regression: a never-encrypted row still gets the old
        (null-polyline-fills-in) upsert behaviour."""
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(id=333, user_info_id=uid, name="Plain", type="Ride",
                                 kudos_count=1,
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            sess.commit()
        fresh = _make_activity(id=333, name="Plain Renamed", kudos_count=5,
                                summary_polyline="freshpoly")
        with Session(engine) as sess:
            repo._upsert_activity(sess, uid, fresh)
            sess.commit()
        with Session(engine) as sess:
            row = sess.get(DBActivity, 333)
            assert row.name == "Plain Renamed"
            assert row.kudos_count == 5
            assert row.summary_polyline == "freshpoly"


class TestUpdateActivityEnrichmentGuard:
    """ActivityMixin.update_activity_enrichment — background stream enrichment."""

    def test_encrypted_polyline_and_elevation_survive_enrichment(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            repo.update_activity_enrichment(
                sess, 111, "freshpoly",
                json.dumps({"distances_km": [0, 1], "elevations_m": [1, 2]}),
                owner_id=uid,
            )
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            assert row.summary_polyline == _ENC_POLY
            assert row.elevation_profile_json == _ENC_ELEV
            assert row.elevation_profile_low_res_json == _ENC_LOWRES

    def test_enrichment_still_updates_when_not_encrypted(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(id=222, user_info_id=uid, name="Plain", type="Ride",
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            sess.commit()
        with Session(engine) as sess:
            repo.update_activity_enrichment(
                sess, 222, "freshpoly",
                json.dumps({"distances_km": [0, 1], "elevations_m": [1, 2]}),
                owner_id=uid,
            )
        with Session(engine) as sess:
            row = sess.get(DBActivity, 222)
            assert row.summary_polyline == "freshpoly"
            assert json.loads(row.elevation_profile_json) == {"distances_km": [0, 1], "elevations_m": [1, 2]}
            assert row.elevation_profile_low_res_json is not None


class TestForceUpdateActivityGuard:
    """ActivityMixin.force_update_activity — explicit user "force refresh"."""

    def test_encrypted_fields_survive_but_scalars_refresh(self, repo_env):
        engine, uid, repo = repo_env
        fresh = _make_activity(
            id=111, name="Renamed", kudos_count=77,
            start_latlng=[1.0, 2.0], end_latlng=[3.0, 4.0],
            summary_polyline="freshpoly",
            elevation_profile=([0.0, 1.0], [5.0, 6.0]),
        )
        with Session(engine) as sess:
            repo.force_update_activity(sess, uid, fresh)
        with Session(engine) as sess:
            row = sess.get(DBActivity, 111)
            # Encrypted geometry/name fields are untouched.
            assert row.name == _ENC_NAME
            assert row.start_latlng_json == _ENC_START
            assert row.end_latlng_json == _ENC_END
            assert row.summary_polyline == _ENC_POLY
            assert row.elevation_profile_json == _ENC_ELEV
            assert row.elevation_profile_low_res_json == _ENC_LOWRES
            # Non-encrypted scalar fields DO still refresh.
            assert row.kudos_count == 77

    def test_plaintext_activity_fully_refreshes(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(id=444, user_info_id=uid, name="Plain", type="Ride",
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            sess.commit()
        fresh = _make_activity(id=444, name="Refreshed", kudos_count=9,
                                summary_polyline="brandnewpoly")
        with Session(engine) as sess:
            repo.force_update_activity(sess, uid, fresh)
        with Session(engine) as sess:
            row = sess.get(DBActivity, 444)
            assert row.name == "Refreshed"
            assert row.summary_polyline == "brandnewpoly"
            assert row.kudos_count == 9


# ---------------------------------------------------------------------------
# 3. Server-side "must not crash on ciphertext" guards
# ---------------------------------------------------------------------------

def _project_with_encrypted_activity(*, plaintext_id=None) -> Project:
    """A Project with one activity whose geometry is fully encrypted
    (start_latlng/end_latlng/summary_polyline all None, *_enc set instead —
    exactly what ActivityMixin._row_to_activity() now produces), optionally
    alongside a normal plaintext activity for contrast."""
    enc_act = Activity(
        id=111, name="Encrypted Ride", type="Ride", distance=1000.0,
        moving_time=100, elapsed_time=100, total_elevation_gain=0.0,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        start_date_local=datetime(2026, 1, 1, tzinfo=timezone.utc),
        timezone="UTC", achievement_count=0, kudos_count=0, comment_count=0,
        athlete_count=1, photo_count=0, trainer=False, commute=False, manual=False,
        private=False, flagged=False, average_speed=0.0, max_speed=0.0,
        has_heartrate=False, pr_count=0, total_photo_count=0, has_kudoed=False,
        start_latlng=None, end_latlng=None, summary_polyline=_ENC_POLY,
        elevation_profile=None,
        start_latlng_enc=_ENC_START, end_latlng_enc=_ENC_END,
        elevation_profile_enc=_ENC_ELEV,
    )
    activities = [enc_act]
    items = [ProjectItem(item_type="activity", activity_id=111)]
    if plaintext_id is not None:
        plain_act = _make_activity(id=plaintext_id, name="Plain Ride")
        activities.append(plain_act)
        items.append(ProjectItem(item_type="activity", activity_id=plaintext_id))
    project = Project(name="Trip", items=items, activities=activities)
    project.rebuild_map()
    return project


class TestGeoBuildersSkipEncryptedActivity:
    def test_full_res_geo_skips_encrypted_activity_without_crash(self):
        project = _project_with_encrypted_activity(plaintext_id=222)
        features = _build_full_geo_features(project)
        act_features = [f for f in features if f["properties"]["type"] == "activity"]
        assert len(act_features) == 1
        assert act_features[0]["properties"]["activity_id"] == 222

    def test_full_res_geo_all_encrypted_yields_no_crash_and_no_features(self):
        project = _project_with_encrypted_activity()
        features = _build_full_geo_features(project)
        assert features == []

    def test_share_build_features_skips_encrypted_activity_without_crash(self):
        project = _project_with_encrypted_activity(plaintext_id=222)
        features = _build_features(project)
        act_features = [f for f in features if f["properties"]["type"] == "activity"]
        assert len(act_features) == 1
        assert act_features[0]["properties"]["activity_id"] == 222


class TestLowResGeoAndFullLoadDoNotCrash(object):
    """Integration-level: DB round trip through ActivityMixin._row_to_activity,
    which is where json.loads() on a ciphertext envelope would otherwise raise."""

    def test_low_res_geo_endpoint_skips_encrypted_activity(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            proj = DBProject(user_info_id=uid, name="Trip")
            sess.add(proj)
            sess.commit()
            sess.refresh(proj)
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                    item_type="activity", activity_id=111))
            sess.commit()
        client = _client(engine, uid, geo_router)
        resp = client.get("/api/geo/project/low-res?name=Trip")
        assert resp.status_code == 200
        feats = [f for f in resp.json()["features"] if f["properties"]["type"] == "activity"]
        assert feats == []

    def test_full_res_geo_endpoint_skips_encrypted_activity(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            proj = DBProject(user_info_id=uid, name="Trip")
            sess.add(proj)
            sess.commit()
            sess.refresh(proj)
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                    item_type="activity", activity_id=111))
            sess.commit()
        client = _client(engine, uid, geo_router)
        resp = client.get("/api/geo/project?name=Trip")
        assert resp.status_code == 200
        feats = [f for f in resp.json()["features"] if f["properties"]["type"] == "activity"]
        assert feats == []

    def test_project_get_does_not_crash_on_encrypted_activity(self, repo_env):
        """Regression guard for the deeper root cause: json.loads() on
        start_latlng_json/elevation_profile_json used to run unconditionally in
        _row_to_activity(), so simply LOADING a project with an encrypted
        activity (not just building geo) would raise before this fix."""
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            proj = DBProject(user_info_id=uid, name="Trip")
            sess.add(proj)
            sess.commit()
            sess.refresh(proj)
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                    item_type="activity", activity_id=111))
            sess.commit()
        from api.projects import router as projects_router
        client = _client(engine, uid, projects_router)
        resp = client.get("/api/projects/Trip")
        assert resp.status_code == 200
        acts = {a["id"]: a for a in resp.json()["activities"]}
        assert acts[111]["name"] == _ENC_NAME
        assert acts[111]["start_latlng"] is None
        assert acts[111]["start_latlng_enc"] == _ENC_START
        assert acts[111]["elevation_profile_enc"] == _ENC_ELEV


class TestGpxExportGuard:
    def test_export_returns_409_for_project_with_encrypted_activity(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            proj = DBProject(user_info_id=uid, name="Trip")
            sess.add(proj)
            sess.commit()
            sess.refresh(proj)
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                    item_type="activity", activity_id=111))
            sess.commit()
        client = _client(engine, uid, project_transfer_router)
        resp = client.get("/api/projects/Trip/export")
        assert resp.status_code == 409

    def test_export_still_works_for_plaintext_project(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(
                id=222, user_info_id=uid, name="Plain Ride", type="Ride",
                summary_polyline=polyline_lib.encode(_TRACK),
                start_date="2026-01-01T00:00:00Z",
                start_date_local="2026-01-01T00:00:00Z",
            ))
            proj = DBProject(user_info_id=uid, name="PlainTrip")
            sess.add(proj)
            sess.commit()
            sess.refresh(proj)
            sess.add(DBProjectItem(project_id=proj.id, position=0,
                                    item_type="activity", activity_id=222))
            sess.commit()
        client = _client(engine, uid, project_transfer_router)
        resp = client.get("/api/projects/PlainTrip/export")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4. New PUT /api/activities/{id} field-update endpoint
# ---------------------------------------------------------------------------

class TestActivityFieldsUpdateEndpoint:
    def test_updates_only_the_given_fields(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(id=555, user_info_id=uid, name="Plain", type="Ride",
                                 summary_polyline="plainpoly", kudos_count=3,
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            sess.commit()
        client = _client(engine, uid, activity_fields_router)
        resp = client.put("/api/activities/555", json={
            "name": _ENC_NAME,
            "summary_polyline": _ENC_POLY,
        })
        assert resp.status_code == 200, resp.text
        with Session(engine) as sess:
            row = sess.get(DBActivity, 555)
            assert row.name == _ENC_NAME
            assert row.summary_polyline == _ENC_POLY
            # Untouched fields (not present in the request) are unaffected.
            assert row.kudos_count == 3
            assert row.type == "Ride"

    def test_idempotent_reencrypting_same_value(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(id=666, user_info_id=uid, name="Plain", type="Ride",
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            sess.commit()
        client = _client(engine, uid, activity_fields_router)
        body = {"name": _ENC_NAME}
        assert client.put("/api/activities/666", json=body).status_code == 200
        resp2 = client.put("/api/activities/666", json=body)
        assert resp2.status_code == 200
        with Session(engine) as sess:
            assert sess.get(DBActivity, 666).name == _ENC_NAME

    def test_404_for_missing_activity(self, repo_env):
        engine, uid, repo = repo_env
        client = _client(engine, uid, activity_fields_router)
        resp = client.put("/api/activities/999999", json={"name": _ENC_NAME})
        assert resp.status_code == 404

    def test_404_for_another_users_activity(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            other = UserInfo(display_name="Bob", email="bob@b.c")
            sess.add(other)
            sess.commit()
            sess.refresh(other)
            other_uid = other.id
            sess.add(DBActivity(id=777, user_info_id=other_uid, name="Bob's Ride", type="Ride",
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            sess.commit()
        client = _client(engine, uid, activity_fields_router)
        resp = client.put("/api/activities/777", json={"name": _ENC_NAME})
        assert resp.status_code == 404
        with Session(engine) as sess:
            assert sess.get(DBActivity, 777).name == "Bob's Ride"

    def test_bumps_lock_version_for_every_project_containing_the_activity(self, repo_env):
        """A native client's on-disk cache only ever checks lock_version, so
        the ciphertext swap must advance it (issue #173) — same signal every
        other activity-mutating path already bumps."""
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(id=999, user_info_id=uid, name="Plain", type="Ride",
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            proj = DBProject(user_info_id=uid, name="Trip")
            sess.add(proj)
            sess.commit()
            sess.refresh(proj)
            project_id = proj.id
            sess.add(DBProjectItem(project_id=project_id, position=0,
                                   item_type="activity", activity_id=999))
            sess.commit()
            before = proj.lock_version

        client = _client(engine, uid, activity_fields_router)
        resp = client.put("/api/activities/999", json={"name": _ENC_NAME})
        assert resp.status_code == 200, resp.text

        with Session(engine) as sess:
            assert sess.get(DBProject, project_id).lock_version == before + 1

    def test_original_snapshot_columns_can_be_scrubbed(self, repo_env):
        engine, uid, repo = repo_env
        with Session(engine) as sess:
            sess.add(DBActivity(id=888, user_info_id=uid, name="Plain", type="Ride",
                                 original_polyline="origpoly",
                                 original_elevation_profile_json="origep",
                                 start_date="2026-01-01T00:00:00Z",
                                 start_date_local="2026-01-01T00:00:00Z"))
            sess.commit()
        client = _client(engine, uid, activity_fields_router)
        resp = client.put("/api/activities/888", json={
            "original_polyline": _ENC_POLY,
            "original_elevation_profile_json": _ENC_ELEV,
        })
        assert resp.status_code == 200
        with Session(engine) as sess:
            row = sess.get(DBActivity, 888)
            assert row.original_polyline == _ENC_POLY
            assert row.original_elevation_profile_json == _ENC_ELEV
