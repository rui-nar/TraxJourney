"""Importing a trip respects the plan's trip-length limit (#492).

``/import`` and ``/import-zip`` used to check only the number of trips. A trip
longer than the plan allows could be imported whole. Now, in every mode
(create, copy, replace), the span is measured on the written rows just before
the commit, with the same definition every other trip-length check uses:

* longer than the limit and longer than the trip was: 402 ``trip_days``,
  nothing created or changed, no photo placed, no staging directory left;
* a trip already over the limit (a downgrade) can still be replaced by its own
  export, or by a shorter one;
* with quotas off, everything goes through.

Free trips are capped at 10 days here.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as memories_mod
import api.project_shared as project_shared_mod
import api.project_transfer as transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBJournalEntry, DBMemory, DBProject, DBProjectItem
from models.user import UserInfo
from src.project.project_io import ProjectIO

_PHOTO = "00000000-0000-4000-8000-000000000492"
_MEMORY_ID = 7


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app as one user, a file-backed DB, files under tmp_path."""
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, transfer_mod, storage_mod, journal_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREE_MAX_TRIP_DAYS", "10")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        uid = user.id

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        yield TestClient(router.app, raise_server_exceptions=False), engine, uid, tmp_path
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


@pytest.fixture(params=["traxj", "zip"])
def route(request):
    return request.param


def _enforcing(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_PROJECTS", "10")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _doc(*dates: str, trip_start: str | None = None, photo: bool = False) -> bytes:
    """A trip of one memory per date. With *photo*, the first memory names a
    photo, which the ZIP carries."""
    items = []
    for n, day in enumerate(dates):
        memory = {"name": f"Day {day}", "date": day, "public_id": f"pub-{day}",
                  "photos": [], "geo_mode": "custom", "lat": 45.9, "lon": 6.8}
        if n == 0 and photo:
            memory["id"] = _MEMORY_ID
            memory["photos"] = [_PHOTO]
        items.append({"item_type": "memory", "memory": memory})
    doc = {"version": 1, "name": "x", "items": items, "activities": []}
    if trip_start is not None:
        doc["trip_start"] = trip_start
    return json.dumps(doc).encode("utf-8")


def _zip(trip: bytes, photo: bool) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"Alps{ProjectIO.EXTENSION}", trip)
        if photo:
            zf.writestr(f"photos/{_MEMORY_ID}/{_PHOTO}.jpg", _jpeg())
    return buf.getvalue()


def _import(client, route: str, trip: bytes, on_conflict: str | None = None,
            photo: bool = False):
    """Import *trip* as "Alps" through *route*; the ZIP carries the photo."""
    params = {"on_conflict": on_conflict} if on_conflict else {}
    if route == "traxj":
        return client.post("/api/projects/import", params=params, files={
            "file": (f"Alps{ProjectIO.EXTENSION}", trip, "application/json")})
    return client.post("/api/projects/import-zip", params=params, files={
        "file": ("Alps.zip", _zip(trip, photo), "application/zip")})


def _export(client, route: str) -> bytes:
    """The trip "Alps" exported in the format *route* imports."""
    path = "export-traxj" if route == "traxj" else "export-zip"
    r = client.get(f"/api/projects/Alps/{path}")
    assert r.status_code == 200, r.text
    return r.content


def _post_export(client, route: str, exported: bytes, on_conflict: str):
    if route == "traxj":
        return _import(client, route, exported, on_conflict)
    return client.post("/api/projects/import-zip", params={"on_conflict": on_conflict},
                       files={"file": ("Alps.zip", exported, "application/zip")})


def _snapshot(engine) -> dict:
    """Every row an import writes, column by column."""
    with Session(engine) as sess:
        return {model.__name__: sorted(
                    (repr(sorted(row.model_dump().items()))
                     for row in sess.exec(select(model)).all()))
                for model in (DBProject, DBMemory, DBJournalEntry, DBProjectItem)}


def _names(engine, uid) -> set[str]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBProject.name).where(DBProject.user_info_id == uid)))


def _dates(engine, uid, name="Alps") -> list[str]:
    with Session(engine) as sess:
        trip = sess.exec(select(DBProject).where(
            DBProject.user_info_id == uid, DBProject.name == name)).one()
        return sorted(m.date for m in sess.exec(
            select(DBMemory).where(DBMemory.project_id == trip.id)))


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def _staging_dirs(data: Path) -> list[Path]:
    tmp = data / "tmp"
    return [p for p in tmp.iterdir() if p.name.startswith("import-")] if tmp.exists() else []


def _assert_refused(r, engine, data, before_rows, before_files, needed: int):
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["resource"] == "trip_days"
    assert (body["limit"], body["needed"]) == (10, needed)
    assert _snapshot(engine) == before_rows
    assert _files(data / "users") == before_files
    assert _staging_dirs(data) == []


# 2024-06-01 .. 2024-06-11 is 11 days: one past the Free limit.
_LONG = ("2024-06-01", "2024-06-11")


# ── Refused ──────────────────────────────────────────────────────────────────

def test_a_new_trip_longer_than_the_plan_is_refused(env, route, monkeypatch):
    client, engine, uid, data = env
    _enforcing(monkeypatch)
    rows, files = _snapshot(engine), _files(data / "users")

    r = _import(client, route, _doc(*_LONG, photo=True), photo=True)

    _assert_refused(r, engine, data, rows, files, needed=11)
    assert _names(engine, uid) == set()


def test_a_new_trip_exactly_at_the_limit_is_imported(env, route, monkeypatch):
    client, engine, uid, data = env
    _enforcing(monkeypatch)

    r = _import(client, route, _doc("2024-06-01", "2024-06-10"))

    assert r.status_code == 201, r.text
    assert _names(engine, uid) == {"Alps"}


def test_a_copy_longer_than_the_plan_is_refused(env, route, monkeypatch):
    client, engine, uid, data = env
    assert _import(client, route, _doc("2024-06-01")).status_code == 201
    _enforcing(monkeypatch)
    rows, files = _snapshot(engine), _files(data / "users")

    r = _import(client, route, _doc(*_LONG, photo=True), on_conflict="copy", photo=True)

    _assert_refused(r, engine, data, rows, files, needed=11)
    assert _names(engine, uid) == {"Alps"}


def test_a_replace_that_makes_the_trip_too_long_is_refused(env, route, monkeypatch):
    client, engine, uid, data = env
    assert _import(client, route, _doc("2024-06-01", "2024-06-02")).status_code == 201
    _enforcing(monkeypatch)
    rows, files = _snapshot(engine), _files(data / "users")

    r = _import(client, route, _doc(*_LONG, photo=True), on_conflict="replace", photo=True)

    _assert_refused(r, engine, data, rows, files, needed=11)
    assert _dates(engine, uid) == ["2024-06-01", "2024-06-02"]


def test_a_replace_is_measured_on_what_it_writes_not_on_the_file(env, route, monkeypatch):
    """The file carries no trip_start, so the trip keeps its own, and that
    date still counts: on its own the file is one day long."""
    client, engine, uid, data = env
    doc = _doc("2024-05-20", trip_start="2024-05-20")
    assert _import(client, route, doc).status_code == 201
    _enforcing(monkeypatch)
    rows, files = _snapshot(engine), _files(data / "users")
    without_start = json.loads(_doc("2024-06-05"))
    assert "trip_start" not in without_start

    r = _import(client, route, json.dumps(without_start).encode(), on_conflict="replace")

    _assert_refused(r, engine, data, rows, files, needed=17)


# ── Allowed ──────────────────────────────────────────────────────────────────

def test_a_trip_already_too_long_can_be_replaced_by_its_own_export(env, route, monkeypatch):
    """A downgrade: the trip was imported before the limit applied to it."""
    client, engine, uid, data = env
    assert _import(client, route, _doc("2024-06-01", "2024-06-20")).status_code == 201
    exported = _export(client, route)
    _enforcing(monkeypatch)

    r = _post_export(client, route, exported, "replace")

    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "replaced"
    assert _dates(engine, uid) == ["2024-06-01", "2024-06-20"]


def test_a_replace_that_shortens_a_too_long_trip_is_imported(env, route, monkeypatch):
    client, engine, uid, data = env
    assert _import(client, route, _doc("2024-06-01", "2024-06-20")).status_code == 201
    _enforcing(monkeypatch)

    # 15 days: still over the limit, but shorter than the 20 it was.
    r = _import(client, route, _doc("2024-06-01", "2024-06-15"), on_conflict="replace")

    assert r.status_code == 201, r.text
    assert _dates(engine, uid) == ["2024-06-01", "2024-06-15"]


def test_with_quotas_off_every_mode_imports_a_long_trip(env, route):
    client, engine, uid, data = env

    created = _import(client, route, _doc(*_LONG, photo=True), photo=True)
    copied = _import(client, route, _doc(*_LONG), on_conflict="copy")
    replaced = _import(client, route, _doc("2024-06-01", "2024-06-30"), on_conflict="replace")

    assert [r.status_code for r in (created, copied, replaced)] == [201, 201, 201]
    assert [r.json()["outcome"] for r in (created, copied, replaced)] == [
        "created", "copied", "replaced"]
    assert _names(engine, uid) == {"Alps", "Alps (2)"}
    assert _dates(engine, uid) == ["2024-06-01", "2024-06-30"]
