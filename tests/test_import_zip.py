"""``POST /api/projects/import-zip`` imports a trip ZIP with its photos (#469).

A ``.traxj`` carries photo names but no photo files, so a trip moved between
accounts lost every photo. The ZIP export carries them, and now imports back:

* under the same name and conflict rules as ``/import`` (409, copy, replace),
  the name conflict and the trip limit settled before the archive is read;
* all or nothing: every photo is decoded and staged in
  ``<data dir>/tmp/import-<uuid>/`` before any row is written, moved into place
  only once the trip has committed, and the staging directory always goes;
* within the storage quota, counting what will be placed: on Replace the
  photos the trip already has are not counted again, nor touched;
* a photo that cannot be placed after the commit loses its name from its row
  under the row's photo lock, keeping whatever was added meanwhile.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import time
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
import src.project.photo_placement as placement_mod
from api.deps import get_current_user
from api.photo_locks import photo_lock
from models.billing import UserUsage
from models.project_db import DBJournalEntry, DBMemory, DBProject
from models.user import UserInfo
from src.project.project_io import ProjectIO

_MB = 1024 * 1024


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, two accounts, a file-backed DB, files under tmp_path."""
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, transfer_mod, storage_mod, journal_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("owner", "other")}
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
               engine, ids, act_as, tmp_path)
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _jpeg(color, size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _trip(memory_photos=(), journal_photos=(), memory_id=None, journal_id=None) -> bytes:
    """A trip of one memory and one journal entry."""
    memory = {"name": "Lac Blanc", "date": "2024-06-01", "public_id": "pub-lac-blanc",
              "photos": list(memory_photos), "geo_mode": "custom", "lat": 45.98, "lon": 6.89}
    journal = {"date": "2024-06-01", "description": "Tired legs",
               "photos": list(journal_photos), "geo_mode": "end_of_day"}
    if memory_id is not None:
        memory["id"] = memory_id
    if journal_id is not None:
        journal["id"] = journal_id
    return json.dumps({
        "version": 1, "name": "x",
        "items": [{"item_type": "memory", "memory": memory},
                  {"item_type": "journal", "journal": journal}],
        "activities": [],
    }).encode("utf-8")


def _import_traxj(client, name: str, content: bytes):
    return client.post(
        "/api/projects/import",
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _import_zip(client, filename: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    return client.post("/api/projects/import-zip", params=params,
                       files={"file": (filename, content, "application/zip")})


def _upload(client, kind: str, content_id: int, color) -> str:
    r = client.post(f"/api/{kind}/{content_id}/photos",
                    files={"file": ("p.jpg", _jpeg(color), "image/jpeg")})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _rows(engine, uid, name):
    """(memory row, journal row) of the user's trip *name*."""
    with Session(engine) as sess:
        trip = sess.exec(select(DBProject).where(
            DBProject.user_info_id == uid, DBProject.name == name)).one()
        mem = sess.exec(select(DBMemory).where(DBMemory.project_id == trip.id)).one()
        jnl = sess.exec(select(DBJournalEntry).where(DBJournalEntry.project_id == trip.id)).one()
        return mem, jnl


def _names(engine, uid) -> set[str]:
    with Session(engine) as sess:
        return set(sess.exec(select(DBProject.name).where(DBProject.user_info_id == uid)))


def _row_counts(engine) -> tuple[int, int, int]:
    with Session(engine) as sess:
        return (len(sess.exec(select(DBProject)).all()),
                len(sess.exec(select(DBMemory)).all()),
                len(sess.exec(select(DBJournalEntry)).all()))


def _usage(engine, uid) -> int:
    with Session(engine) as sess:
        row = sess.exec(select(UserUsage).where(UserUsage.user_info_id == uid)).first()
        return row.storage_bytes if row else 0


def _set_usage(engine, uid, used: int) -> None:
    with Session(engine) as sess:
        row = sess.exec(select(UserUsage).where(UserUsage.user_info_id == uid)).first()
        row = row or UserUsage(user_info_id=uid)
        row.storage_bytes = used
        sess.add(row)
        sess.commit()


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def _staging_dirs(data: Path) -> list[Path]:
    tmp = data / "tmp"
    return [p for p in tmp.iterdir() if p.name.startswith("import-")] if tmp.exists() else []


def _source(client, act_as):
    """The owner's trip "Alps", with two memory photos and one journal photo,
    and its ZIP export. Returns (zip bytes, memory id, journal id, memory
    photos, journal photos)."""
    act_as("owner")
    assert _import_traxj(client, "Alps", _trip()).status_code == 201
    r = client.get("/api/projects/Alps")
    items = r.json()["items"]
    (mid,) = [i["memory"]["id"] for i in items if i["item_type"] == "memory"]
    (jid,) = [i["journal"]["id"] for i in items if i["item_type"] == "journal"]
    mem = [_upload(client, "memories", mid, (200, 30, 30)),
           _upload(client, "memories", mid, (30, 200, 30))]
    jnl = [_upload(client, "journal", jid, (30, 30, 200))]
    r = client.get("/api/projects/Alps/export-zip")
    assert r.status_code == 200, r.text
    return r.content, mid, jid, mem, jnl


def _make_zip(trip: bytes, entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"Alps{ProjectIO.EXTENSION}", trip)
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _placed(data: Path, uid: int, kind: str, row_id: int, photos) -> int:
    """Assert each photo's full file and a regenerated thumbnail are in place;
    return their bytes."""
    folder = data / "users" / str(uid) / kind / str(row_id)
    total = 0
    for u in photos:
        full, thumb = folder / f"{u}.jpg", folder / f"{u}_thumb.jpg"
        assert full.is_file() and thumb.is_file(), (full, thumb)
        with Image.open(thumb) as im:
            assert im.format == "JPEG" and max(im.size) <= 400
        total += full.stat().st_size + thumb.stat().st_size
    return total


# ── Create, copy, replace ─────────────────────────────────────────────────────

def test_a_new_name_imports_the_trip_with_every_photo(env):
    client, engine, ids, act_as, data = env
    archive, mid, jid, mem, jnl = _source(client, act_as)
    owner_mem = data / "users" / str(ids["owner"]) / "memories" / str(mid)
    # A thumbnail in the archive is never used: the import makes its own.
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        trip = zf.read(f"Alps{ProjectIO.EXTENSION}")
        photos = {n: zf.read(n) for n in zf.namelist() if n.endswith(".jpg")}
    photos[f"photos/{mid}/{mem[0]}_thumb.jpg"] = b"not a thumbnail"
    archive = _make_zip(trip, photos)

    act_as("other")
    before = _usage(engine, ids["other"])
    r = _import_zip(client, "Alps.zip", archive)

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "created"}
    m, j = _rows(engine, ids["other"], "Alps")
    assert json.loads(m.photos_json) == mem
    assert json.loads(j.photos_json) == jnl
    placed = (_placed(data, ids["other"], "memories", m.id, mem)
              + _placed(data, ids["other"], "journal", j.id, jnl))
    for u in mem:
        assert (data / "users" / str(ids["other"]) / "memories" / str(m.id)
                / f"{u}.jpg").read_bytes() == (owner_mem / f"{u}.jpg").read_bytes()
    assert _usage(engine, ids["other"]) - before == placed
    assert _staging_dirs(data) == []
    # The photos open through the API, full size and thumbnail.
    assert client.get(f"/api/memories/{m.id}/photos/{mem[0]}").status_code == 200
    assert client.get(f"/api/memories/{m.id}/photos/{mem[0]}/thumb").status_code == 200


def test_an_export_unzipped_and_zipped_again_imports_with_its_photos(env):
    """Finder's "Compress" of the unzipped export: every file under one
    folder, plus __MACOSX/ AppleDouble files (owner decision, 2026-10-03)."""
    client, engine, ids, act_as, data = env
    archive, _mid, _jid, mem, jnl = _source(client, act_as)
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as src, zipfile.ZipFile(buf, "w") as dst:
        dst.writestr("Alps/", b"")
        for name in src.namelist():
            dst.writestr(f"Alps/{name}", src.read(name))
            dst.writestr(f"__MACOSX/Alps/._{name.rsplit('/', 1)[-1]}", b"\x00\x05\x16\x07")

    act_as("other")
    r = _import_zip(client, "Alps.zip", buf.getvalue())

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "created"}
    m, j = _rows(engine, ids["other"], "Alps")
    assert json.loads(m.photos_json) == mem and json.loads(j.photos_json) == jnl
    _placed(data, ids["other"], "memories", m.id, mem)
    _placed(data, ids["other"], "journal", j.id, jnl)
    assert _staging_dirs(data) == []


def test_keep_both_imports_a_copy_with_its_own_photos(env):
    client, engine, ids, act_as, data = env
    archive, _mid, _jid, mem, jnl = _source(client, act_as)
    act_as("other")
    assert _import_zip(client, "Alps.zip", archive).status_code == 201
    first = _rows(engine, ids["other"], "Alps")
    before = _usage(engine, ids["other"])

    r = _import_zip(client, "Alps.zip", archive, on_conflict="copy")

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps (2)", "outcome": "copied"}
    m, j = _rows(engine, ids["other"], "Alps (2)")
    assert (m.id, j.id) != (first[0].id, first[1].id)
    assert json.loads(m.photos_json) == mem and json.loads(j.photos_json) == jnl
    placed = (_placed(data, ids["other"], "memories", m.id, mem)
              + _placed(data, ids["other"], "journal", j.id, jnl))
    assert _usage(engine, ids["other"]) - before == placed


def test_replace_places_the_photos_the_trip_lacks(env):
    client, engine, ids, act_as, data = env
    archive, mid, jid, mem, jnl = _source(client, act_as)
    act_as("other")
    # The same trip, without its photos: a .traxj import stores no name.
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert _import_traxj(client, "Alps", zf.read(f"Alps{ProjectIO.EXTENSION}")).status_code == 201
    kept = _rows(engine, ids["other"], "Alps")
    assert json.loads(kept[0].photos_json) == []
    before = _usage(engine, ids["other"])

    r = _import_zip(client, "Alps.zip", archive, on_conflict="replace")

    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Alps", "outcome": "replaced"}
    m, j = _rows(engine, ids["other"], "Alps")
    assert m.id == kept[0].id
    assert json.loads(m.photos_json) == mem and json.loads(j.photos_json) == jnl
    placed = (_placed(data, ids["other"], "memories", m.id, mem)
              + _placed(data, ids["other"], "journal", j.id, jnl))
    assert _usage(engine, ids["other"]) - before == placed


def test_replace_with_the_trip_s_own_zip_leaves_its_photos_alone(env, monkeypatch):
    client, engine, ids, act_as, data = env
    archive, mid, jid, mem, jnl = _source(client, act_as)
    owner = data / "users" / str(ids["owner"])
    files = sorted(_files(owner))
    stats = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size) for p in files}
    # At the storage limit: nothing new is stored, so nothing is refused.
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
    _set_usage(engine, ids["owner"], _MB)

    r = _import_zip(client, "Alps.zip", archive, on_conflict="replace")

    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "replaced"
    assert sorted(_files(owner)) == files
    assert {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size)
            for p in files} == stats
    assert _usage(engine, ids["owner"]) == _MB
    m, j = _rows(engine, ids["owner"], "Alps")
    assert (m.id, j.id) == (mid, jid)
    assert json.loads(m.photos_json) == mem and json.loads(j.photos_json) == jnl


def test_replace_keeps_a_photo_placed_in_a_reused_row_id(env):
    """Review U2R1-1. Replacing trip X with the ZIP of its Keep-both copy
    deletes X's memory and journal entry (the copy's have other public ids
    and ids) and creates new ones. While X's rows hold the highest ids,
    SQLite gives the new rows those same ids, with the same photo names: a
    removal and a placement then name the same files, and only removing
    before placing leaves the photos in place."""
    client, engine, ids, act_as, data = env
    archive, mid, jid, mem, jnl = _source(client, act_as)
    assert _import_zip(client, "Alps.zip", archive, on_conflict="copy").status_code == 201
    r = client.get("/api/projects/Alps (2)/export-zip")
    assert r.status_code == 200, r.text
    copy_archive = r.content
    # The copy goes again, so X's rows hold the highest ids once more.
    assert client.delete("/api/projects/Alps (2)").status_code == 204

    r = _import_zip(client, "Alps.zip", copy_archive, on_conflict="replace")

    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "replaced"
    m, j = _rows(engine, ids["owner"], "Alps")
    assert (m.id, j.id) == (mid, jid)  # the ids were reused: the case under test
    assert json.loads(m.photos_json) == mem and json.loads(j.photos_json) == jnl
    _placed(data, ids["owner"], "memories", m.id, mem)
    _placed(data, ids["owner"], "journal", j.id, jnl)
    on_disk = sum(p.stat().st_size for p in _files(data / "users" / str(ids["owner"])))
    assert _usage(engine, ids["owner"]) == on_disk


def _over_the_limit(monkeypatch, engine, uid) -> None:
    """An account over its 1 MB storage limit, say after its plan lapsed."""
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_PROJECTS", "10")
    monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
    _set_usage(engine, uid, 2 * _MB)


def test_over_the_limit_a_replace_that_stores_nothing_is_accepted(env, monkeypatch):
    """Review U4R1-1: restoring a trip from its own ZIP adds no byte."""
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)
    _over_the_limit(monkeypatch, engine, ids["owner"])

    r = _import_zip(client, "Alps.zip", archive, on_conflict="replace")

    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "replaced"
    assert _usage(engine, ids["owner"]) == 2 * _MB


def test_over_the_limit_a_zip_without_photos_is_accepted(env, monkeypatch):
    client, engine, ids, act_as, data = env
    act_as("other")
    _over_the_limit(monkeypatch, engine, ids["other"])

    r = _import_zip(client, "Plain.zip", _make_zip(_trip(), {}))

    assert r.status_code == 201, r.text
    assert _names(engine, ids["other"]) == {"Plain"}
    assert _usage(engine, ids["other"]) == 2 * _MB


def test_over_the_limit_a_zip_with_new_photos_is_refused(env, monkeypatch):
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)
    act_as("other")
    _over_the_limit(monkeypatch, engine, ids["other"])

    r = _import_zip(client, "Alps.zip", archive)

    assert r.status_code == 402, r.text
    assert r.json()["resource"] == "storage"
    assert _names(engine, ids["other"]) == set()
    assert _files(data / "users" / str(ids["other"])) == set()


# ── Refusals ─────────────────────────────────────────────────────────────────

def test_a_taken_name_is_refused_before_the_archive_is_read(env, monkeypatch):
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)

    def _must_not_read(*_a, **_k):
        raise AssertionError("the archive was read")

    monkeypatch.setattr(transfer_mod, "read_trip_zip", _must_not_read)

    r = _import_zip(client, "Alps.zip", archive)

    assert r.status_code == 409, r.text
    assert r.json()["code"] == "name_conflict" and r.json()["name"] == "Alps"
    assert _staging_dirs(data) == []


def test_over_the_storage_quota_nothing_is_created(env, monkeypatch):
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)
    act_as("other")
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
    _set_usage(engine, ids["other"], _MB - 100)  # room for less than the photos
    counts = _row_counts(engine)

    r = _import_zip(client, "Alps.zip", archive)

    assert r.status_code == 402, r.text
    assert r.json()["resource"] == "storage"
    assert _names(engine, ids["other"]) == set()
    assert _row_counts(engine) == counts
    assert _files(data / "users" / str(ids["other"])) == set()
    assert _staging_dirs(data) == []
    assert _usage(engine, ids["other"]) == _MB - 100


def test_keep_both_of_the_trip_s_own_zip_at_the_storage_limit_is_refused(env, monkeypatch):
    """A copy stores every photo again, so none is subtracted (R2-1)."""
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)
    owner = data / "users" / str(ids["owner"])
    files = _files(owner)
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_PROJECTS", "10")
    monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
    _set_usage(engine, ids["owner"], _MB)
    counts = _row_counts(engine)

    r = _import_zip(client, "Alps.zip", archive, on_conflict="copy")

    assert r.status_code == 402, r.text
    assert r.json()["resource"] == "storage"
    assert _names(engine, ids["owner"]) == {"Alps"}
    assert _row_counts(engine) == counts
    assert _files(owner) == files
    assert _staging_dirs(data) == []


@pytest.mark.parametrize("archive", [
    pytest.param(b"this is not a zip archive", id="not-a-zip"),
    pytest.param(_make_zip(_trip(memory_photos=["00000000-0000-4000-8000-000000000469"],
                                 memory_id=7),
                           {"photos/7/00000000-0000-4000-8000-000000000469.jpg": b"no image"}),
                 id="not-an-image"),
])
def test_a_bad_archive_or_image_is_refused_with_400_and_leaves_nothing(env, archive):
    client, engine, ids, act_as, data = env
    act_as("other")
    counts = _row_counts(engine)

    r = _import_zip(client, "Alps.zip", archive)

    assert r.status_code == 400, r.text
    assert r.json()["detail"]
    assert _row_counts(engine) == counts
    assert _files(data / "users") == set()
    assert _staging_dirs(data) == []


def test_only_a_zip_is_accepted(env):
    client, *_ = env
    r = _import_zip(client, f"Alps{ProjectIO.EXTENSION}", _trip())
    assert r.status_code == 400
    assert ".zip" in r.json()["detail"]


def test_a_failed_commit_leaves_no_photo_and_no_staging_directory(env, monkeypatch):
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)
    act_as("other")
    repo = project_shared_mod._repo
    real_write = repo._write_content

    def _write_then_fail(*args, **kwargs):
        real_write(*args, **kwargs)
        raise RuntimeError("disk I/O error")  # the commit never happens

    monkeypatch.setattr(repo, "_write_content", _write_then_fail)
    counts = _row_counts(engine)

    r = _import_zip(client, "Alps.zip", archive)

    assert r.status_code == 500
    assert _row_counts(engine) == counts
    assert _files(data / "users" / str(ids["other"])) == set()
    assert _staging_dirs(data) == []
    assert transfer_mod._import_guard.acquire(blocking=False)
    transfer_mod._import_guard.release()


# ── The cap ──────────────────────────────────────────────────────────────────

def test_the_zip_cap_is_one_gigabyte():
    assert transfer_mod.MAX_ZIP_IMPORT_BYTES == 1024 * 1024 * 1024
    assert transfer_mod._too_large(transfer_mod.MAX_ZIP_IMPORT_BYTES).detail.endswith("1 GB.")


_BOUNDARY = b"zip-cap-boundary"
_CT = (b"content-type", b"multipart/form-data; boundary=" + _BOUNDARY)


def _multipart(file_bytes: int, chunk: int = 4096) -> list[bytes]:
    head = (b"--" + _BOUNDARY + b"\r\nContent-Disposition: form-data; name=\"file\"; "
            b"filename=\"Alps.zip\"\r\nContent-Type: application/zip\r\n\r\n")
    body = head + b"x" * file_bytes + b"\r\n--" + _BOUNDARY + b"--\r\n"
    return [body[i:i + chunk] for i in range(0, len(body), chunk)]


def call_asgi(app, path: str, headers, chunks):
    """Drive the ASGI app directly; returns (status, body, chunks consumed)."""
    consumed = 0
    sent: list[dict] = []

    async def receive():
        nonlocal consumed
        if consumed < len(chunks):
            consumed += 1
            return {"type": "http.request", "body": chunks[consumed - 1],
                    "more_body": consumed < len(chunks)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path,
        "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": headers, "client": ("test", 1), "server": ("test", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, consumed, start["headers"]


_SMALL_CAP = 64 * 1024  # stands in for 1 GB, so nothing near it is allocated


def test_a_declared_body_over_the_zip_cap_is_refused_before_it_is_read(env, monkeypatch):
    import api.router as router

    monkeypatch.setattr(transfer_mod, "MAX_ZIP_IMPORT_BYTES", _SMALL_CAP)
    chunks = _multipart(_SMALL_CAP * 4)
    length = str(sum(len(c) for c in chunks)).encode()

    status, body, consumed, _ = call_asgi(
        router.app, "/api/projects/import-zip", [_CT, (b"content-length", length)], chunks)

    assert status == 413
    assert consumed == 0
    assert "too large" in json.loads(body)["detail"]


def test_a_chunked_body_over_the_zip_cap_is_cut_off(env, monkeypatch):
    import api.router as router

    monkeypatch.setattr(transfer_mod, "MAX_ZIP_IMPORT_BYTES", _SMALL_CAP)
    chunks = _multipart(_SMALL_CAP * 20)

    status, _body, consumed, _ = call_asgi(router.app, "/api/projects/import-zip", [_CT], chunks)

    assert status == 413
    assert consumed < len(chunks) // 5
    assert transfer_mod._import_guard.acquire(blocking=False)
    transfer_mod._import_guard.release()


def test_the_zip_cap_does_not_apply_to_a_traxj(env, monkeypatch):
    """Each route keeps its own cap: a .traxj stays at MAX_IMPORT_BYTES."""
    import api.router as router

    monkeypatch.setattr(transfer_mod, "MAX_IMPORT_BYTES", _SMALL_CAP)
    chunks = [c.replace(b"Alps.zip", b"Alps.traxj") for c in _multipart(_SMALL_CAP * 4)]
    length = str(sum(len(c) for c in chunks)).encode()

    status, body, _, _ = call_asgi(
        router.app, "/api/projects/import", [_CT, (b"content-length", length)], chunks)

    assert status == 413
    assert json.loads(body)["detail"].endswith(f"{_SMALL_CAP // _MB} MB.")


# ── Staging ──────────────────────────────────────────────────────────────────

def test_photos_are_staged_under_the_data_dir_and_read_from_the_spooled_file(env, monkeypatch):
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)
    act_as("other")
    seen = {}
    real = transfer_mod.read_trip_zip

    def _spy(fileobj, staging_dir, **kwargs):
        seen["fileobj"], seen["staging"] = fileobj, Path(staging_dir)
        seen["during"] = sorted(p.name for p in Path(staging_dir).iterdir())
        result = real(fileobj, staging_dir, **kwargs)
        seen["staged"] = _files(Path(staging_dir))
        return result

    monkeypatch.setattr(transfer_mod, "read_trip_zip", _spy)

    assert _import_zip(client, "Alps.zip", archive).status_code == 201

    assert seen["staging"].parent == data / "tmp"
    assert seen["staging"].name.startswith("import-")
    assert not isinstance(seen["fileobj"], (bytes, bytearray))
    assert len(seen["staged"]) == 1 + 2 * 3  # the manifest, then each photo and thumbnail
    assert not seen["staging"].exists()


def test_a_stale_staging_directory_holding_photos_is_logged_and_removed(env, caplog):
    client, engine, ids, act_as, data = env
    archive, *_ = _source(client, act_as)
    act_as("other")
    tmp = data / "tmp"
    stale = tmp / "import-stale"
    (stale / "memories" / "4").mkdir(parents=True)
    manifest = json.dumps({"importer": 99, "trip_name": "Lost trip",
                           "created": "2026-01-01T00:00:00+00:00"})
    (stale / "manifest.json").write_text(manifest, encoding="utf-8")
    (stale / "memories" / "4" / "a.jpg").write_bytes(b"x")
    (stale / "memories" / "4" / "a_thumb.jpg").write_bytes(b"x")
    empty = tmp / "import-empty"
    empty.mkdir()
    (empty / "manifest.json").write_text("{}", encoding="utf-8")
    fresh = tmp / "import-fresh"
    (fresh / "memories").mkdir(parents=True)
    (fresh / "memories" / "b.jpg").write_bytes(b"x")
    two_days = time.time() - 2 * 24 * 3600
    for d in (stale, empty):
        os.utime(d, (two_days, two_days))

    with caplog.at_level(logging.ERROR, logger=transfer_mod.__name__):
        assert _import_zip(client, "Alps.zip", archive).status_code == 201

    assert not stale.exists() and not empty.exists()
    assert fresh.exists()  # not a day old: may be in use
    errors = [r.getMessage() for r in caplog.records
              if r.levelno == logging.ERROR and r.name == transfer_mod.__name__]
    assert len(errors) == 1, errors
    assert "import-stale" in errors[0]
    assert "2 photo files" in errors[0]
    assert "48." in errors[0] or "47." in errors[0]  # its age in hours
    assert manifest in errors[0]


# ── A photo that cannot be placed after the commit ────────────────────────────

def test_a_photo_that_cannot_be_placed_loses_only_its_name(env, monkeypatch, caplog):
    client, engine, ids, act_as, data = env
    archive, _mid, _jid, mem, jnl = _source(client, act_as)
    act_as("other")
    lost = mem[0]
    real_replace = os.replace

    def _replace(src, dst):
        if Path(dst).name == f"{lost}.jpg":
            raise OSError("disk error")
        return real_replace(src, dst)

    monkeypatch.setattr(placement_mod.os, "replace", _replace)
    real_place = transfer_mod.place_photos

    def _place_then_companion_adds(data_dir, importer, placements):
        failed = real_place(data_dir, importer, placements)
        # A companion uploads to the same memory between commit and follow-up.
        row_id = next(p.row_id for p in placements if p.kind == "memories")
        with Session(db_module.engine) as sess:
            row = sess.get(DBMemory, row_id)
            row.photos_json = json.dumps(json.loads(row.photos_json) + ["added-meanwhile"])
            sess.add(row)
            sess.commit()
        return failed

    monkeypatch.setattr(transfer_mod, "place_photos", _place_then_companion_adds)

    with caplog.at_level(logging.ERROR, logger=transfer_mod.__name__):
        r = _import_zip(client, "Alps.zip", archive)

    assert r.status_code == 201, r.text
    m, j = _rows(engine, ids["other"], "Alps")
    assert json.loads(m.photos_json) == [mem[1], "added-meanwhile"]
    assert json.loads(j.photos_json) == jnl
    folder = data / "users" / str(ids["other"]) / "memories" / str(m.id)
    assert not (folder / f"{lost}.jpg").exists()
    (rec,) = [r for r in caplog.records
              if r.levelno == logging.ERROR and r.name == transfer_mod.__name__]
    message = rec.getMessage()
    assert lost in message and str(m.id) in message and "'memory'" in message
    assert _staging_dirs(data) == []


@pytest.mark.parametrize("kind,lock_key,model", [
    ("memories", "memory", DBMemory),
    ("journal", "journal", DBJournalEntry),
])
def test_the_follow_up_waits_for_the_row_s_photo_lock(env, kind, lock_key, model):
    client, engine, ids, act_as, data = env
    act_as("owner")
    assert _import_traxj(client, "Alps", _trip()).status_code == 201
    m, j = _rows(engine, ids["owner"], "Alps")
    row_id = m.id if kind == "memories" else j.id
    with Session(engine) as sess:
        row = sess.get(model, row_id)
        row.photos_json = json.dumps(["keep", "drop"])
        sess.add(row)
        sess.commit()

    def _photos():
        with Session(engine) as sess:
            return json.loads(sess.get(model, row_id).photos_json)

    lock = photo_lock(lock_key, row_id)
    lock.acquire()
    try:
        worker = threading.Thread(
            target=transfer_mod._drop_unplaced, args=(kind, row_id, "drop"))
        worker.start()
        worker.join(timeout=0.5)
        assert worker.is_alive()  # blocked on the lock uploads take
        assert _photos() == ["keep", "drop"]
    finally:
        lock.release()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert _photos() == ["keep"]
