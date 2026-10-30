"""A Replace import is refused while the trip's photos are being upgraded
(issue #33, D13, U15).

An apply of "Upgrade photos from Immich" lists the original A' in place of A,
commits, and only then deletes A's files. A Replace import in that window
still finds A's file, lists A again, and deletes A' as dropped; the apply then
deletes A's files, and the photo is gone. So while any apply on the trip is
``pending`` or ``running``, whoever's it is, a Replace of the trip, ZIP or
.traxj, answers 409 with the code ``photo_upgrade_running`` and writes
nothing:

* ``replace_project`` checks right after ``bump_lock_version``, inside the
  write transaction, where no apply can start or commit until the import
  does. The refusal rolls the bump back;
* the ZIP route checks once more before reading the archive, so a large
  upload is refused before minutes of reading.

An import under a new name, or as a copy, creates a trip and is never refused.
"""

from __future__ import annotations

import io
import json
import sqlite3
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
import src.project.repo_transfer as repo_transfer_mod
from api.deps import get_current_user
from models.project_db import (
    DBJournalEntry, DBMemory, DBPhotoUpgradeScan, DBProject, DBProjectItem,
)
from models.user import UserInfo
from src.project.project_io import ProjectIO

_DETAIL = ("Photos on this trip are being upgraded from Immich. "
           "Try again once that has finished.")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, an owner and a companion, a file-backed DB, files under
    tmp_path."""
    import api.router as router

    db_path = tmp_path / "app.db"
    engine = db_module._make_engine(f"sqlite:///{db_path.as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, transfer_mod, storage_mod, journal_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com")
                 for n in ("owner", "companion")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}

    current = {"uid": ids["owner"]}
    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}

    def act_as(who: str) -> None:
        current["uid"] = ids[who]

    try:
        yield (TestClient(router.app, raise_server_exceptions=False),
               engine, ids, act_as, tmp_path, db_path)
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


def _trip() -> bytes:
    """A trip of one memory and one journal entry."""
    return json.dumps({
        "version": 1, "name": "x",
        "items": [
            {"item_type": "memory", "memory": {
                "name": "Lac Blanc", "date": "2024-06-01", "public_id": "pub-lac-blanc",
                "photos": [], "geo_mode": "custom", "lat": 45.98, "lon": 6.89}},
            {"item_type": "journal", "journal": {
                "date": "2024-06-01", "description": "Tired legs",
                "photos": [], "geo_mode": "end_of_day"}}],
        "activities": [],
    }).encode("utf-8")


def _import_traxj(client, name: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    return client.post(
        "/api/projects/import", params=params,
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _import_zip(client, name: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    return client.post("/api/projects/import-zip", params=params,
                       files={"file": (f"{name}.zip", content, "application/zip")})


def _upload(client, kind: str, content_id: int, color) -> str:
    r = client.post(f"/api/{kind}/{content_id}/photos",
                    files={"file": ("p.jpg", _jpeg(color), "image/jpeg")})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _edited(archive: bytes) -> tuple[bytes, bytes]:
    """The archive, and its .traxj, with the journal entry's text changed:
    a Replace that went through would show."""
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    traxj_name = next(n for n in entries if n.endswith(ProjectIO.EXTENSION))
    doc = json.loads(entries[traxj_name])
    for item in doc["items"]:
        if item["item_type"] == "journal":
            item["journal"]["description"] = "Replaced"
    entries[traxj_name] = json.dumps(doc).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, data in entries.items():
            zf.writestr(n, data)
    return buf.getvalue(), entries[traxj_name]


def _scan(engine, project_id: int, uid: int, apply_status: str) -> None:
    with Session(engine) as sess:
        sess.add(DBPhotoUpgradeScan(
            project_id=project_id, user_info_id=uid, album_id="album-1",
            status="ready", apply_status=apply_status))
        sess.commit()


def _set_apply(db_path: Path, apply_status: str) -> None:
    """Every scan's apply_status, through a connection of its own, as the
    worker or another request would."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE photoupgradescan SET apply_status = ?", (apply_status,))
        conn.commit()
    finally:
        conn.close()


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def _staging_dirs(data: Path) -> list[Path]:
    tmp = data / "tmp"
    return [p for p in tmp.iterdir() if p.name.startswith("import-")] if tmp.exists() else []


def _state(engine, data: Path, project_id: int, owner: int):
    """Everything a Replace would change: the trip row's lock version and
    content stamp, its memory, journal and item rows with their photo lists,
    and the owner's files."""
    with Session(engine) as sess:
        trip = sess.get(DBProject, project_id)
        rows = [
            (type(r).__name__, r.id, getattr(r, "description", None),
             r.photos_json, r.photo_order_json)
            for model in (DBMemory, DBJournalEntry)
            for r in sess.exec(select(model).where(model.project_id == project_id)
                               .order_by(model.id))
        ]
        items = [(i.id, i.item_type, i.position) for i in sess.exec(
            select(DBProjectItem).where(DBProjectItem.project_id == project_id)
            .order_by(DBProjectItem.id))]
        return (trip.lock_version, trip.updated_at, rows, items,
                _files(data / "users" / str(owner)))


@pytest.fixture
def alps(env):
    """The owner's trip "Alps" with a memory photo and a journal photo, a
    companion (editor) with their own journal entry, the trip's ZIP export
    with the owner's journal text changed, and that export's .traxj."""
    client, engine, ids, act_as, data, db_path = env
    act_as("owner")
    assert _import_traxj(client, "Alps", _trip()).status_code == 201
    items = client.get("/api/projects/Alps").json()["items"]
    (mid,) = [i["memory"]["id"] for i in items if i["item_type"] == "memory"]
    (jid,) = [i["journal"]["id"] for i in items if i["item_type"] == "journal"]
    _upload(client, "memories", mid, (200, 30, 30))
    _upload(client, "journal", jid, (30, 30, 200))
    r = client.get("/api/projects/Alps/export-zip")
    assert r.status_code == 200, r.text
    archive, traxj = _edited(r.content)

    token = client.post("/api/projects/Alps/members/invite").json()["token"]
    act_as("companion")
    assert client.post(f"/api/invites/{token}/accept").status_code == 200
    r = client.post(f"/api/journal/?owner={ids['owner']}", json={
        "project_name": "Alps", "date": "2024-06-01", "geo_mode": "custom",
        "lat": 45.0, "lon": 6.0, "description": "companion private note"})
    assert r.status_code == 201, r.text
    act_as("owner")

    with Session(engine) as sess:
        project_id = sess.exec(select(DBProject.id).where(
            DBProject.user_info_id == ids["owner"], DBProject.name == "Alps")).one()
    return client, engine, ids, act_as, data, db_path, project_id, archive, traxj


def _refused(r) -> None:
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "photo_upgrade_running"
    assert body["detail"] == _DETAIL
    assert "request_id" in body


def _replaced(r) -> None:
    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "replaced"}


# ── Refused ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("apply_status", ["pending", "running"])
def test_a_zip_replace_is_refused_while_an_apply_is_in_progress(alps, apply_status, monkeypatch):
    client, engine, ids, _, data, _, project_id, archive, _ = alps
    _scan(engine, project_id, ids["owner"], apply_status)
    before = _state(engine, data, project_id, ids["owner"])

    def _must_not_read(*_a, **_k):
        raise AssertionError("the archive was read")

    monkeypatch.setattr(transfer_mod, "read_trip_zip", _must_not_read)

    r = _import_zip(client, "Alps", archive, on_conflict="replace")

    _refused(r)
    assert _state(engine, data, project_id, ids["owner"]) == before
    assert _staging_dirs(data) == []


@pytest.mark.parametrize("apply_status", ["pending", "running"])
def test_a_zip_replace_is_refused_when_an_apply_starts_while_it_is_read(
        alps, apply_status, monkeypatch):
    """The route's early check passed; replace_project's own check, under the
    write lock, decides. The staged photos go."""
    client, engine, ids, _, data, db_path, project_id, archive, _ = alps
    _scan(engine, project_id, ids["owner"], "idle")
    before = _state(engine, data, project_id, ids["owner"])
    real_read = transfer_mod.read_trip_zip
    staged = []

    def _read_then_apply(*args, **kwargs):
        result = real_read(*args, **kwargs)
        staged.extend(_files(args[1]))
        _set_apply(db_path, apply_status)
        return result

    monkeypatch.setattr(transfer_mod, "read_trip_zip", _read_then_apply)

    r = _import_zip(client, "Alps", archive, on_conflict="replace")

    _refused(r)
    assert staged, "the archive's photos were staged"
    assert _state(engine, data, project_id, ids["owner"]) == before
    assert _staging_dirs(data) == []
    assert transfer_mod._import_guard.acquire(blocking=False)
    transfer_mod._import_guard.release()


@pytest.mark.parametrize("apply_status", ["pending", "running"])
def test_a_traxj_replace_is_refused_while_an_apply_is_in_progress(alps, apply_status):
    client, engine, ids, _, data, _, project_id, _, traxj = alps
    _scan(engine, project_id, ids["owner"], apply_status)
    before = _state(engine, data, project_id, ids["owner"])

    r = _import_traxj(client, "Alps", traxj, on_conflict="replace")

    _refused(r)
    # Refused after bump_lock_version: the bump is rolled back with the rest.
    assert _state(engine, data, project_id, ids["owner"]) == before


def test_a_companion_s_apply_blocks_the_owner_s_replace(alps):
    client, engine, ids, _, data, _, project_id, archive, traxj = alps
    _scan(engine, project_id, ids["owner"], "done")
    _scan(engine, project_id, ids["companion"], "running")
    before = _state(engine, data, project_id, ids["owner"])

    _refused(_import_zip(client, "Alps", archive, on_conflict="replace"))
    _refused(_import_traxj(client, "Alps", traxj, on_conflict="replace"))

    assert _state(engine, data, project_id, ids["owner"]) == before
    assert _staging_dirs(data) == []


def test_the_check_runs_under_the_write_lock_after_the_bump(alps, monkeypatch):
    """When replace_project checks, it has advanced lock_version, and holds
    SQLite's write lock: an apply cannot become pending until it is done."""
    client, engine, ids, _, data, db_path, project_id, _, traxj = alps
    _scan(engine, project_id, ids["owner"], "idle")
    with Session(engine) as sess:
        version = sess.get(DBProject, project_id).lock_version
    real_check = repo_transfer_mod.photo_upgrade_running
    seen = []

    def _probe(sess, pid):
        seen.append(sess.exec(select(DBProject.lock_version).where(
            DBProject.id == pid)).one())
        other = sqlite3.connect(db_path, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE photoupgradescan SET apply_status = 'pending'")
        finally:
            other.close()
        return real_check(sess, pid)

    monkeypatch.setattr(repo_transfer_mod, "photo_upgrade_running", _probe)

    _replaced(_import_traxj(client, "Alps", traxj, on_conflict="replace"))

    assert seen == [version + 1]


# ── Allowed ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("apply_status", ["idle", "stopped", "done"])
def test_a_replace_goes_through_when_no_apply_is_in_progress(alps, apply_status):
    client, engine, ids, _, data, _, project_id, archive, traxj = alps
    _scan(engine, project_id, ids["owner"], apply_status)
    _scan(engine, project_id, ids["companion"], apply_status)

    _replaced(_import_zip(client, "Alps", archive, on_conflict="replace"))
    _replaced(_import_traxj(client, "Alps", traxj, on_conflict="replace"))

    with Session(engine) as sess:
        notes = sess.exec(select(DBJournalEntry.description).where(
            DBJournalEntry.project_id == project_id)).all()
    assert "Replaced" in notes


@pytest.mark.parametrize("route", ["zip", "traxj"])
@pytest.mark.parametrize("on_conflict, name, outcome", [
    (None, "Elsewhere", "created"),
    ("copy", "Alps", "copied"),
])
def test_an_import_that_creates_a_trip_is_never_refused(
        alps, route, on_conflict, name, outcome):
    client, engine, ids, _, data, _, project_id, archive, traxj = alps
    _scan(engine, project_id, ids["owner"], "running")
    _scan(engine, project_id, ids["companion"], "pending")

    if route == "zip":
        r = _import_zip(client, name, archive, on_conflict=on_conflict)
    else:
        r = _import_traxj(client, name, traxj, on_conflict=on_conflict)

    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == outcome
