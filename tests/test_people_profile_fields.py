"""API tests for person profile fields — socials, nationalities, residence (#49).

Covers the new fields round-tripping through create/get/update, the polarsteps
column being mirrored from the socials list (so the shared-trip view keeps
working), and the /api/geo/places city-autocomplete proxy.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.geo as geo_module
import models.db as db_module
from api.deps import get_current_user
from api.geo import router as geo_router
from api.people import router as people_router
from api.projects import router as projects_router
from models.project_db import DBProject
from models.user import UserInfo


def _seed(engine):
    with Session(engine) as sess:
        u = UserInfo(display_name="A", email="a@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        sess.add(DBProject(user_info_id=u.id, name="My Trip")); sess.commit()
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
    app.include_router(people_router)
    app.include_router(projects_router)
    app.include_router(geo_router)
    return TestClient(app), engine, uid


def test_profile_fields_round_trip(env):
    client, _, _ = env
    pid = client.post("/api/people/", json={
        "project_name": "My Trip",
        "name": "Alice",
        "socials": [
            {"network": "instagram", "handle": "alice_ig"},
            {"network": "polarsteps", "handle": "alice_ps"},
        ],
        "nationalities": ["PT", "FR"],
        "residence": "Lisbon, Portugal",
    }).json()["id"]

    body = client.get(f"/api/people/{pid}").json()
    assert body["socials"] == [
        {"network": "instagram", "handle": "alice_ig"},
        {"network": "polarsteps", "handle": "alice_ps"},
    ]
    assert body["nationalities"] == ["PT", "FR"]
    assert body["residence"] == "Lisbon, Portugal"


def test_polarsteps_column_mirrored_from_socials(env):
    """The dedicated polarsteps column must reflect the socials 'polarsteps' entry
    so the shared-trip view (which reads that column) keeps working."""
    client, _, _ = env
    pid = client.post("/api/people/", json={
        "project_name": "My Trip",
        "name": "Alice",
        "socials": [{"network": "polarsteps", "handle": "alice_ps"}],
    }).json()["id"]
    assert client.get(f"/api/people/{pid}").json()["polarsteps"] == "alice_ps"


def test_update_replaces_socials_and_remirrors_polarsteps(env):
    client, _, _ = env
    pid = client.post("/api/people/", json={
        "project_name": "My Trip", "name": "Alice",
        "socials": [{"network": "polarsteps", "handle": "old_ps"}],
    }).json()["id"]

    # Update to socials without a polarsteps entry — the column must clear.
    resp = client.put(f"/api/people/{pid}", json={
        "name": "Alice",
        "socials": [{"network": "strava", "handle": "alice_strava"}],
        "nationalities": ["ES"],
        "residence": "Madrid, Spain",
    })
    assert resp.status_code == 204
    body = client.get(f"/api/people/{pid}").json()
    assert body["socials"] == [{"network": "strava", "handle": "alice_strava"}]
    assert body["polarsteps"] is None
    assert body["nationalities"] == ["ES"]
    assert body["residence"] == "Madrid, Spain"


def test_empty_profile_fields_default_to_empty(env):
    client, _, _ = env
    pid = client.post("/api/people/", json={"project_name": "My Trip", "name": "Bob"}).json()["id"]
    body = client.get(f"/api/people/{pid}").json()
    assert body["socials"] == []
    assert body["nationalities"] == []
    assert body["residence"] is None
    assert body["polarsteps"] is None


# ── /api/geo/places city autocomplete ──────────────────────────────────────────

_FAKE_NOMINATIM = [
    {"name": "Lisbon", "address": {"city": "Lisbon", "country": "Portugal"}},
    {"name": "Lisbon", "address": {"city": "Lisbon", "country": "Portugal"}},  # dupe
    {"name": "Lisbon Falls", "address": {"town": "Lisbon Falls", "country": "United States"}},
    {"name": "Nowhere", "address": {"country": "Portugal"}},  # no city → dropped
]


def test_places_returns_distinct_city_labels(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(geo_module, "_nominatim_search", lambda q: _FAKE_NOMINATIM)
    resp = client.get("/api/geo/places", params={"q": "lisbon"})
    assert resp.status_code == 200
    assert resp.json() == ["Lisbon, Portugal", "Lisbon Falls, United States"]


def test_places_short_query_returns_empty_without_upstream(env, monkeypatch):
    client, _, _ = env
    def _boom(q):  # must not be called for a 1-char query
        raise AssertionError("upstream should not be queried for short input")
    monkeypatch.setattr(geo_module, "_nominatim_search", _boom)
    assert client.get("/api/geo/places", params={"q": "l"}).json() == []


def test_places_upstream_failure_is_502(env, monkeypatch):
    client, _, _ = env
    def _fail(q):
        raise RuntimeError("nominatim down")
    monkeypatch.setattr(geo_module, "_nominatim_search", _fail)
    assert client.get("/api/geo/places", params={"q": "lisbon"}).status_code == 502


class _FakeResponse:
    def __init__(self, payload, status_error=None):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        return self._payload


def test_places_call_is_tracked_via_track_external(env, monkeypatch, metric):
    """The Nominatim call goes through track_external (issue #205) — it must
    get metrics coverage without the endpoint itself changing behaviour."""
    client, _, _ = env
    monkeypatch.setattr(
        geo_module.requests, "get",
        lambda *a, **k: _FakeResponse(_FAKE_NOMINATIM),
    )
    before = metric("traxjourney_external_requests_total",
                     service="nominatim", endpoint="/search", outcome="success")
    resp = client.get("/api/geo/places", params={"q": "lisbon"})
    assert resp.status_code == 200
    assert metric("traxjourney_external_requests_total",
                   service="nominatim", endpoint="/search", outcome="success") - before == 1


def test_places_call_failure_is_tracked_and_logged(env, monkeypatch, metric, caplog):
    client, _, _ = env
    monkeypatch.setattr(
        geo_module.requests, "get",
        lambda *a, **k: _FakeResponse(None, status_error=RuntimeError("nominatim down")),
    )
    before = metric("traxjourney_external_requests_total",
                     service="nominatim", endpoint="/search", outcome="exception")
    with caplog.at_level("WARNING", logger="src.utils.metrics"):
        resp = client.get("/api/geo/places", params={"q": "lisbon"})
    assert resp.status_code == 502
    assert metric("traxjourney_external_requests_total",
                   service="nominatim", endpoint="/search", outcome="exception") - before == 1
    assert "nominatim" in caplog.text


def test_project_io_person_round_trip():
    """The .traxj import/export dicts must carry the new profile fields."""
    from src.models.person import Person
    from src.project.project_io import _person_from_dict, _person_to_dict

    p = Person(
        id=1, name="Alice",
        socials=[{"network": "polarsteps", "handle": "alice_ps"}],
        nationalities=["PT"], residence="Lisbon, Portugal",
    )
    back = _person_from_dict(_person_to_dict(p))
    assert back.socials == p.socials
    assert back.nationalities == p.nationalities
    assert back.residence == p.residence
