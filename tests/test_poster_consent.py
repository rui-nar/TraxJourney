"""Poster consent on encrypted trips, and the scrubbed request a finished job
keeps (docs/E2EE_REMNANTS_PLAN.md, decision 11, unit U5)."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.poster as poster_module
import models.db as db_module
import src.poster.poster_job_runner as job_runner_module
from api.deps import get_current_user
from api.poster import router as poster_router
from models.project_db import DBPosterJob, DBProject
from models.user import UserInfo

_SECRET_NAME = "Secret cove"
_SECRET_NOTE = "Where we hid the boat"
_SECRET_TITLE = "Our hidden summer"
_LAT, _LON = 43.123456, 1.654321


def _body(**over):
    body = {
        "bounds": {"north": 43.2, "south": 43.0, "east": 1.7, "west": 1.6},
        "orientation": "landscape",
        "config": {"memory_text": True},
        "memories": [
            {"id": 7, "lat": _LAT, "lon": _LON, "date": "2024-06-01",
             "name": _SECRET_NAME, "description": _SECRET_NOTE,
             "photo_uuids": ["p1"]},
        ],
        "title_text": _SECRET_TITLE,
    }
    body.update(over)
    return body


class _NoEmail:
    async def send(self, message):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(job_runner_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(job_runner_module, "get_email_service", lambda: _NoEmail())
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        enc = UserInfo(display_name="E", email="e@e.com", encryption_enabled=True)
        plain = UserInfo(display_name="P", email="p@e.com")
        sess.add(enc); sess.add(plain); sess.commit()
        sess.add(DBProject(user_info_id=enc.id, name="Trip"))
        sess.add(DBProject(user_info_id=plain.id, name="Trip"))
        sess.commit()
        ids = {"encrypted": enc.id, "plain": plain.id}

    app = FastAPI()
    who = {"uid": ids["encrypted"]}
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(who["uid"])}
    app.include_router(poster_router)

    def as_user(kind):
        who["uid"] = ids[kind]

    return TestClient(app), engine, as_user


def _jobs(engine):
    with Session(engine) as sess:
        return sess.exec(select(DBPosterJob)).all()


# ── Consent on create ────────────────────────────────────────────────────────

def test_encrypted_owner_with_memory_text_and_no_consent_is_409(env, monkeypatch):
    client, engine, _ = env
    monkeypatch.setattr(poster_module, "run_poster_job", lambda job_id: None)

    r = client.post("/api/projects/Trip/poster", json=_body())

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "consent_required"
    assert detail["consent_required"] == [7]
    # The refusal names ids, never the content.
    assert _SECRET_NAME not in r.text and _SECRET_NOTE not in r.text
    assert _jobs(engine) == []


def test_explicit_false_consent_is_also_409(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(poster_module, "run_poster_job", lambda job_id: None)

    r = client.post("/api/projects/Trip/poster", json=_body(plaintext_consent=False))
    assert r.status_code == 409, r.text


def test_description_alone_needs_consent(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(poster_module, "run_poster_job", lambda job_id: None)
    memory = {"id": 3, "lat": _LAT, "lon": _LON, "date": "2024-06-01",
              "name": None, "description": _SECRET_NOTE}

    r = client.post("/api/projects/Trip/poster", json=_body(memories=[memory]))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["consent_required"] == [3]


def test_encrypted_owner_with_consent_is_accepted(env, monkeypatch):
    client, engine, _ = env
    monkeypatch.setattr(poster_module, "run_poster_job", lambda job_id: None)

    r = client.post("/api/projects/Trip/poster", json=_body(plaintext_consent=True))

    assert r.status_code == 201, r.text
    assert len(_jobs(engine)) == 1


def test_plaintext_owner_needs_no_consent(env, monkeypatch):
    client, engine, as_user = env
    monkeypatch.setattr(poster_module, "run_poster_job", lambda job_id: None)
    as_user("plain")

    r = client.post("/api/projects/Trip/poster", json=_body())

    assert r.status_code == 201, r.text
    assert len(_jobs(engine)) == 1


def test_encrypted_trip_without_memory_text_needs_no_consent(env, monkeypatch):
    client, engine, _ = env
    monkeypatch.setattr(poster_module, "run_poster_job", lambda job_id: None)
    memories = [
        {"id": 1, "lat": _LAT, "lon": _LON, "date": "2024-06-01",
         "photo_uuids": ["p1"]},
        {"id": 2, "lat": _LAT, "lon": _LON, "date": "2024-06-02",
         "name": "", "description": ""},
    ]

    for body in (_body(memories=memories), _body(memories=[])):
        r = client.post("/api/projects/Trip/poster", json=body)
        assert r.status_code == 201, r.text
    assert len(_jobs(engine)) == 2


def test_preview_on_encrypted_trip_needs_no_consent(env, monkeypatch):
    """Decision 11 / R1-7: the preview stores nothing, so it asks nothing."""
    client, engine, _ = env
    monkeypatch.setattr(poster_module, "render_poster_preview",
                        lambda project_id, owner_id, request: (b"\x89PNG", None))

    r = client.post("/api/projects/Trip/poster/preview", json=_body())

    assert r.status_code == 200, r.text
    assert _jobs(engine) == []


# ── Scrubbed request once the job ends ───────────────────────────────────────

def _assert_scrubbed(engine):
    (job,) = _jobs(engine)
    assert job.status in ("done", "failed")
    raw = job.request_json
    for secret in (_SECRET_NAME, _SECRET_NOTE, _SECRET_TITLE, str(_LAT), str(_LON),
                   "2024-06-01"):
        assert secret not in raw
    stored = json.loads(raw)
    assert "bounds" not in stored and "title_text" not in stored
    assert stored["memories"] == [{"id": 7, "photo_uuids": ["p1"]}]
    # Layout choices are kept.
    assert stored["orientation"] == "landscape"
    assert stored["config"]["memory_text"] is True
    return job


def test_done_job_keeps_no_memory_text(env, monkeypatch, tmp_path):
    client, engine, _ = env
    seen = {}

    def _render(*, request, poster_dir, **_kw):
        seen["request"] = request
        png, pdf = poster_dir / "poster.png", poster_dir / "poster.pdf"
        png.write_bytes(b"png"); pdf.write_bytes(b"pdf")
        return png, pdf

    monkeypatch.setattr(job_runner_module, "render_poster", _render)

    r = client.post("/api/projects/Trip/poster", json=_body(plaintext_consent=True))

    assert r.status_code == 201, r.text
    # The renderer still got the full request; only what is kept afterwards shrinks.
    assert seen["request"]["memories"][0]["name"] == _SECRET_NAME
    assert _assert_scrubbed(engine).status == "done"


def test_failed_job_keeps_no_memory_text(env, monkeypatch):
    client, engine, _ = env

    def _boom(**_kw):
        raise RuntimeError("render failed")

    monkeypatch.setattr(job_runner_module, "render_poster", _boom)

    r = client.post("/api/projects/Trip/poster", json=_body(plaintext_consent=True))

    assert r.status_code == 201, r.text
    assert _assert_scrubbed(engine).status == "failed"


def test_orphan_sweep_keeps_no_memory_text(env, monkeypatch):
    """A job failed at startup (worker gone) is scrubbed like any other end."""
    client, engine, _ = env
    monkeypatch.setattr(poster_module, "run_poster_job", lambda job_id: None)
    client.post("/api/projects/Trip/poster", json=_body(plaintext_consent=True))

    assert job_runner_module.sweep_orphaned_poster_jobs() == 1
    assert _assert_scrubbed(engine).status == "failed"


def test_scrub_turns_an_unreadable_request_into_an_empty_one():
    assert job_runner_module.scrub_request("not json") == "{}"
    assert job_runner_module.scrub_request("[1, 2]") == "{}"
    assert job_runner_module.scrub_request(None) == "{}"
