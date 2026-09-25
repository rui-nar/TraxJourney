"""Stored photo names stay inside the folder they belong to.

Memory and journal photos live at ``users/<uid>/memories/<id>/<name>.jpg`` (and
``journal/<id>``), a person's avatar at ``users/<uid>/people/<id>/<name>.jpg``.
The app only ever names them itself, with a uuid. These tests pin that:

* an imported trip file whose photo or avatar names are anything else is
  refused;
* every file operation on a stored name (serving, the public share view, the
  ZIP export, deleting, storage accounting, poster rendering) only ever
  touches a file directly inside that entry's own folder, whatever the name
  holds;
* photos the app did name keep working end to end.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.journal as journal_mod
import api.memories as memories_mod
import api.people as people_mod
import api.project_shared as project_shared_mod
import api.project_transfer as project_transfer_mod
import api.share as share_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBJournalEntry, DBMemory, DBPerson, DBProject
from models.user import UserInfo
from src.project.project_io import ProjectIO

_SECRET = b"not yours"


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, project_transfer_mod, storage_mod, journal_mod,
                memories_mod, people_mod, share_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("alice", "bob")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(ids["alice"])}

    # Bob's files, which nothing Alice does may touch.
    bob_files = []
    for kind in ("memories", "journal", "people"):
        folder = tmp_path / "users" / str(ids["bob"]) / kind / "7"
        folder.mkdir(parents=True)
        for suffix in ("", "_thumb"):
            f = folder / f"victim{suffix}.jpg"
            f.write_bytes(_SECRET)
            bob_files.append(f)

    try:
        yield (TestClient(router.app, raise_server_exceptions=False),
               engine, ids, tmp_path, bob_files)
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _escape(ids, kind: str, sep: str = "/") -> str:
    """A name that, joined under Alice's users/<uid>/<kind>/<id>/, lands on
    Bob's file."""
    return sep.join(["..", "..", "..", str(ids["bob"]), kind, "7", "victim"])


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _assert_bob_untouched(bob_files) -> None:
    for f in bob_files:
        assert f.exists(), f
        assert f.read_bytes() == _SECRET


def _trip(client, name: str = "Mine") -> None:
    assert client.post("/api/projects/", json={"name": name}).status_code == 201


def _memory(client, name: str = "Mine") -> int:
    r = client.post("/api/memories/", json={
        "project_name": name, "name": "Lake", "date": "2024-06-01",
        "geo_mode": "custom", "lat": 45.0, "lon": 6.0})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _journal(client, name: str = "Mine") -> int:
    r = client.post("/api/journal/", json={
        "project_name": name, "date": "2024-06-01", "geo_mode": "custom",
        "lat": 45.0, "lon": 6.0, "description": "note"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _person(client, name: str = "Mine") -> int:
    r = client.post("/api/people/", json={"project_name": name, "name": "Ann"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _set(engine, model, row_id: int, **values) -> None:
    """Store values directly, as data written before names were checked could hold."""
    with Session(engine) as sess:
        row = sess.get(model, row_id)
        for k, v in values.items():
            setattr(row, k, v)
        sess.add(row)
        sess.commit()


# ── Import refuses names the app never makes ────────────────────────────────

def _import(client, doc: dict):
    return client.post("/api/projects/import", files={
        "file": (f"Imported{ProjectIO.EXTENSION}", json.dumps(doc).encode(),
                 "application/json")})


@pytest.mark.parametrize("bad", ["../../../2/memories/7/victim", "..\\..\\x", "..",
                                 "a/b", "", "photo.jpg", 5])
def test_an_import_with_a_memory_photo_name_the_app_never_makes_is_refused(env, bad):
    client, engine, ids, data, bob_files = env
    doc = {"version": 1, "name": "x", "items": [{"item_type": "memory", "memory": {
        "name": "Lake", "date": "2024-06-01", "photos": [bad]}}]}

    r = _import(client, doc)

    assert r.status_code == 400, r.text
    with Session(engine) as sess:
        assert sess.exec(select(DBProject)).all() == []


def test_an_import_with_an_avatar_name_the_app_never_makes_is_refused(env):
    client, engine, ids, data, bob_files = env
    doc = {"version": 1, "name": "x", "items": [],
           "people": [{"id": 1, "name": "Ann", "avatar_photo": _escape(ids, "people")}]}

    assert _import(client, doc).status_code == 400


def test_an_import_with_app_made_names_is_accepted(env):
    client, engine, ids, data, bob_files = env
    name = str(uuid.uuid4())
    doc = {"version": 1, "name": "x",
           "items": [{"item_type": "memory", "memory": {
               "name": "Lake", "date": "2024-06-01", "photos": [name, None]}}],
           "people": [{"id": 1, "name": "Ann", "avatar_photo": str(uuid.uuid4())}]}

    assert _import(client, doc).status_code == 201, _import


# ── File operations stay inside the entry's folder ──────────────────────────

@pytest.mark.parametrize("sep", ["/", "\\"])
def test_the_zip_export_never_reads_outside_a_memorys_folder(env, sep):
    client, engine, ids, data, bob_files = env
    _trip(client)
    mid = _memory(client)
    _set(engine, DBMemory, mid, photos_json=json.dumps([_escape(ids, "memories", sep)]))

    r = client.get("/api/projects/Mine/export-zip")

    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    for info in zf.infolist():
        assert ".." not in info.filename and "\\" not in info.filename, info.filename
        assert zf.read(info) != _SECRET, info.filename


@pytest.mark.parametrize("sep", ["/", "\\"])
def test_deleting_a_memory_never_deletes_outside_its_folder(env, sep):
    client, engine, ids, data, bob_files = env
    _trip(client)
    mid = _memory(client)
    _set(engine, DBMemory, mid, photos_json=json.dumps([_escape(ids, "memories", sep)]))

    assert client.delete(f"/api/memories/{mid}").status_code == 204
    _assert_bob_untouched(bob_files)


@pytest.mark.parametrize("sep", ["/", "\\"])
def test_deleting_a_journal_entry_never_deletes_outside_its_folder(env, sep):
    client, engine, ids, data, bob_files = env
    _trip(client)
    jid = _journal(client)
    _set(engine, DBJournalEntry, jid, photos_json=json.dumps([_escape(ids, "journal", sep)]))

    assert client.delete(f"/api/journal/{jid}").status_code == 204
    _assert_bob_untouched(bob_files)


@pytest.mark.parametrize("sep", ["/", "\\"])
def test_deleting_a_person_or_their_avatar_never_deletes_outside_its_folder(env, sep):
    client, engine, ids, data, bob_files = env
    _trip(client)
    for delete in ("avatar", "person"):
        pid = _person(client)
        _set(engine, DBPerson, pid, avatar_photo=_escape(ids, "people", sep))
        path = f"/api/people/{pid}/avatar" if delete == "avatar" else f"/api/people/{pid}"
        assert client.delete(path).status_code == 204
    _assert_bob_untouched(bob_files)


def test_serving_never_reads_outside_the_entrys_folder(env):
    """A path segment cannot hold "/", but "\\" is a separator on Windows."""
    client, engine, ids, data, bob_files = env
    _trip(client)
    mid, jid, pid = _memory(client), _journal(client), _person(client)
    mem_name = _escape(ids, "memories", "\\")
    _set(engine, DBMemory, mid, photos_json=json.dumps([mem_name]))
    _set(engine, DBJournalEntry, jid, photos_json=json.dumps([_escape(ids, "journal", "\\")]))
    _set(engine, DBPerson, pid, avatar_photo=_escape(ids, "people", "\\"))
    token = client.post("/api/projects/Mine/share").json()["share_token"]

    urls = [f"/api/memories/{mid}/photos/{quote(mem_name, safe='')}",
            f"/api/memories/{mid}/photos/{quote(mem_name, safe='')}/thumb",
            f"/api/journal/{jid}/photos/{quote(_escape(ids, 'journal', chr(92)), safe='')}",
            f"/api/journal/{jid}/photos/{quote(_escape(ids, 'journal', chr(92)), safe='')}/thumb",
            f"/api/people/{pid}/avatar",
            f"/api/people/{pid}/avatar/thumb",
            f"/api/share/{token}/photos/{mid}/{quote(mem_name, safe='')}",
            f"/api/share/{token}/photos/{mid}/{quote(mem_name, safe='')}/thumb"]
    for url in urls:
        r = client.get(url)
        assert r.status_code == 404, (url, r.status_code)
        assert r.content != _SECRET, url


def test_the_poster_never_reads_a_photo_outside_the_memorys_folder(env):
    from src.poster.poster_renderer import _photo_resolver

    client, engine, ids, data, bob_files = env
    resolve = _photo_resolver(str(ids["alice"]), 3)
    for sep in ("/", "\\"):
        assert resolve(_escape(ids, "memories", sep)) is None


def test_storage_accounting_never_deletes_outside_the_users_folder(env):
    from src.billing.usage import unlink_and_record

    client, engine, ids, data, bob_files = env
    mine = data / "users" / str(ids["alice"]) / "memories" / "3"
    mine.mkdir(parents=True)

    unlink_and_record(ids["alice"], [mine / ".." / ".." / ".." / str(ids["bob"])
                                     / "memories" / "7" / "victim.jpg", *bob_files])

    _assert_bob_untouched(bob_files)


# ── Photos the app named still work end to end ──────────────────────────────

def test_an_uploaded_photo_serves_exports_shares_and_deletes(env):
    client, engine, ids, data, bob_files = env
    _trip(client)
    mid = _memory(client)
    r = client.post(f"/api/memories/{mid}/photos",
                    files={"file": ("p.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 201, r.text
    name = r.json()["uuid"]

    assert client.get(f"/api/memories/{mid}/photos/{name}").status_code == 200
    assert client.get(f"/api/memories/{mid}/photos/{name}/thumb").status_code == 200
    token = client.post("/api/projects/Mine/share").json()["share_token"]
    assert client.get(f"/api/share/{token}/photos/{mid}/{name}").status_code == 200
    assert client.get(f"/api/share/{token}/photos/{mid}/{name}/thumb").status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(client.get("/api/projects/Mine/export-zip").content))
    assert f"photos/{mid}/{name}.jpg" in zf.namelist()

    folder = data / "users" / str(ids["alice"]) / "memories" / str(mid)
    assert (folder / f"{name}.jpg").exists()
    assert client.delete(f"/api/memories/{mid}/photos/{name}").status_code == 204
    assert not (folder / f"{name}.jpg").exists()
    assert not (folder / f"{name}_thumb.jpg").exists()


def test_an_uploaded_avatar_and_journal_photo_serve_and_delete(env):
    client, engine, ids, data, bob_files = env
    _trip(client)
    pid, jid = _person(client), _journal(client)
    assert client.post(f"/api/people/{pid}/avatar",
                       files={"file": ("a.jpg", _jpeg(), "image/jpeg")}).status_code == 201
    assert client.get(f"/api/people/{pid}/avatar").status_code == 200
    assert client.get(f"/api/people/{pid}/avatar/thumb").status_code == 200
    r = client.post(f"/api/journal/{jid}/photos",
                    files={"file": ("j.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 201, r.text
    name = r.json()["uuid"]
    assert client.get(f"/api/journal/{jid}/photos/{name}").status_code == 200
    assert client.get(f"/api/journal/{jid}/photos/{name}/thumb").status_code == 200

    assert client.delete(f"/api/journal/{jid}").status_code == 204
    assert client.delete(f"/api/people/{pid}").status_code == 204
    users = data / "users" / str(ids["alice"])
    assert not list((users / "journal").rglob("*.jpg"))
    assert not list((users / "people").rglob("*.jpg"))


# ── Names stored before they were checked ───────────────────────────────────

def test_the_audit_lists_stored_names_the_app_never_makes(env):
    from scripts.audit_photo_names import find_bad_photo_names

    client, engine, ids, data, bob_files = env
    _trip(client)
    mid, jid, pid = _memory(client), _journal(client), _person(client)
    good = str(uuid.uuid4())
    _set(engine, DBMemory, mid, photos_json=json.dumps([good, None, "../x"]))
    _set(engine, DBJournalEntry, jid, photos_json=json.dumps(["a\b"]))
    _set(engine, DBPerson, pid, avatar_photo="..")

    with Session(engine) as sess:
        found = find_bad_photo_names(sess)

    assert [(f["kind"], f["id"], f["name"]) for f in found] == [
        ("memory", mid, "../x"), ("journal", jid, "a\b"), ("person avatar", pid, "..")]
