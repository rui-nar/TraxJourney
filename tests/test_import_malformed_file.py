"""A malformed ``.traxj`` upload is the uploader's fault: 400, not 500 (issue #451).

``POST /api/projects/import`` used to let a ``UnicodeDecodeError``,
``JSONDecodeError`` or the ``AttributeError`` of a wrong-shaped document reach
the catch-all handler: an ERROR log line for a routine user mistake, and
"Internal server error" on the user's screen instead of being told the file is
not a trip.

Run through the real app, so the catch-all and its ERROR line are in play. Each
malformed file must answer 400 with a message the client shows as-is, log
nothing at ERROR, and ingest nothing. A genuine server bug must still be a 500:
only faults in the document itself are the uploader's.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBActivity, DBProject
from models.user import UserInfo
from src.brand import APP_NAME
from src.models.memory import Memory
from src.project.project_io import ProjectIO

_INVALID = f"This file isn't a valid {APP_NAME} trip"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, one signed-in user, an in-memory DB."""
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(project_shared_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        uid = user.id

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        # A 500 must come back as a response, not re-raise into the test.
        yield TestClient(router.app, raise_server_exceptions=False), engine
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _trip(**overrides) -> dict:
    """A well-formed trip holding one activity, so a partial ingest would show."""
    doc = {
        "version": 1,
        "name": "Trip",
        "trip_start": None,
        "filter_state": {"start_date": None, "end_date": None, "activity_types": None},
        "items": [{"item_type": "activity", "activity_id": 1}],
        "activities": [{
            "id": 1, "name": "Ride", "type": "Ride",
            "start_date": "2024-06-01T08:00:00Z",
        }],
    }
    doc.update(overrides)
    return doc


def _json(doc) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _import(client, content: bytes):
    return client.post(
        "/api/projects/import",
        files={"file": (f"Trip{ProjectIO.EXTENSION}", content, "application/json")},
    )


def _count(engine, model) -> int:
    with Session(engine) as sess:
        return sess.exec(select(func.count()).select_from(model)).one()


_MALFORMED = {
    "empty file": b"",
    "truncated": _json(_trip())[:40],
    "not JSON at all": b"PK\x03\x04 this is a zip, renamed",
    "Latin-1 text": json.dumps(_trip(name="Café"), ensure_ascii=False).encode("latin-1"),
    "UTF-16 with a BOM": json.dumps(_trip()).encode("utf-16"),
    "UTF-16 without a BOM": json.dumps(_trip()).encode("utf-16-le"),
    "nested past the parser's depth": b"[" * 200_000 + b"]" * 200_000,
    # Python refuses to convert an integer this long (a plain ValueError,
    # not a JSONDecodeError).
    "an integer past the parser's digit limit": (
        b'{"items": [], "version": ' + b"1" * 5000 + b"}"
    ),
    "a JSON array": _json([_trip()]),
    "a JSON string": _json("trip"),
    "JSON null": b"null",
    "an object that is not a trip": _json({"type": "FeatureCollection", "features": []}),
    "items is not a list": _json(_trip(items={"0": {"item_type": "activity"}})),
    "items is null": _json(_trip(items=None)),
    "an item is not an object": _json(_trip(items=["activity"])),
    "an item_type is not text": _json(_trip(items=[{"item_type": ["memory"]}])),
    "a memory is not an object": _json(_trip(items=[{"item_type": "memory", "memory": "x"}])),
    "a journal is not an object": _json(_trip(items=[{"item_type": "journal", "journal": 1}])),
    "an encounter is not an object": _json(_trip(items=[{"item_type": "encounter", "encounter": []}])),
    "a segment is not an object": _json(_trip(items=[{"item_type": "segment", "segment": "x"}])),
    "a segment end is not an object": _json(_trip(items=[{"segment": {"start": {}, "end": [1, 2]}}])),
    "activities is not a list": _json(_trip(activities=7)),
    "an activity id is a list": _json(_trip(activities=[{"id": [1], "name": "Ride"}])),
    "an activity id is text": _json(_trip(activities=[{"id": "one", "name": "Ride"}])),
    "filter_state is not an object": _json(_trip(filter_state=["2024-01-01"])),
    "day_meta is not an object": _json(_trip(day_meta=[{"sleeping": "tent"}])),
    "a day_meta entry is not an object": _json(_trip(day_meta={"2024-06-01": "tent"})),
    "people is not a list": _json(_trip(people="Ann")),
    "a person is not an object": _json(_trip(people=["Ann"])),
    "a group is not an object": _json(_trip(groups=[3])),
}


@pytest.mark.parametrize("content", _MALFORMED.values(), ids=_MALFORMED.keys())
def test_a_malformed_file_is_refused_with_a_message_and_nothing_else(env, caplog, content):
    client, engine = env

    with caplog.at_level(logging.DEBUG):
        r = _import(client, content)

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail.startswith(_INVALID), detail
    # The client lifts the detail out with a "no double quote" pattern
    # (ProjectsNotifier._msg), so one inside it would cut the message short.
    assert '"' not in detail, detail
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert _count(engine, DBProject) == 0
    assert _count(engine, DBActivity) == 0


def test_a_utf8_bom_is_accepted(env):
    """Windows tools (PowerShell 5's UTF8, older Notepad) prefix UTF-8 with a
    BOM. The text is still UTF-8, byte for byte; only the signature is extra."""
    client, engine = env

    r = _import(client, b"\xef\xbb\xbf" + _json(_trip()))

    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Trip"
    assert _count(engine, DBProject) == 1
    assert _count(engine, DBActivity) == 1


def test_a_bug_while_reading_a_valid_file_is_still_a_500(env, caplog, monkeypatch):
    """Only a fault in the document is the uploader's. A crash in the code
    reading a well-formed file is ours, and must stay loud."""
    client, engine = env

    def _bug(_d):
        raise TypeError("bug in the reader")

    monkeypatch.setattr(Memory, "from_dict", staticmethod(_bug))
    doc = _trip(items=[{"item_type": "memory", "memory": {"name": "Lake"}}])

    with caplog.at_level(logging.ERROR):
        r = _import(client, _json(doc))

    assert r.status_code == 500
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)
    assert _count(engine, DBProject) == 0


def test_a_bug_during_ingest_is_still_a_500(env, caplog, monkeypatch):
    client, engine = env

    def _bug(*_a, **_kw):
        raise TypeError("bug in ingest")

    monkeypatch.setattr(project_shared_mod._repo, "ingest_project", _bug)

    with caplog.at_level(logging.ERROR):
        r = _import(client, _json(_trip()))

    assert r.status_code == 500
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)
