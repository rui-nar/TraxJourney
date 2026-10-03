"""The ZIP export carries the whole trip and its photos (#469).

The trip file inside the ZIP used to be a cut-down document: no people,
groups, day notes, sleeping options or settings, and no journal photos. It is
now exactly what the ``.traxj`` export writes, plus ``photo_refs`` naming each
memory's and journal entry's photos in the archive:

* memory photos at ``photos/{memory id}/{uuid}.jpg``, from the trip owner's
  folder, where every editor's memory photos live (#106);
* journal photos at ``journal/{entry id}/{uuid}.jpg``, from the caller's own
  folder: a journal is private to its author, and the export holds only the
  caller's own entries.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.journal as journal_mod
import api.memories as memories_mod
import api.project_shared as project_shared_mod
import api.project_transfer as project_transfer_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from models.project_db import DBProject, DBProjectMember
from models.user import UserInfo
from src.project.project_io import ProjectIO
from tests.test_export_compact import _content, _trip


@pytest.fixture
def env(monkeypatch, tmp_path):
    """An owner and a companion (an editor of the owner's trips), files under
    tmp_path."""
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, project_transfer_mod, storage_mod,
                journal_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
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
               engine, ids, act_as, tmp_path)
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "JPEG")
    return buf.getvalue()


def _alps(activities: int, points: int) -> bytes:
    """test_export_compact's trip of one of everything, its memory without
    photos: the ones this file names were never uploaded here."""
    doc = json.loads(_trip(activities, points))
    for item in doc["items"]:
        if item["item_type"] == "memory":
            item["memory"]["photos"] = []
    return json.dumps(doc).encode("utf-8")


def _import(client, name: str, content: bytes):
    return client.post(
        "/api/projects/import",
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _upload(client, kind: str, content_id: int, color) -> str:
    r = client.post(f"/api/{kind}/{content_id}/photos",
                    files={"file": ("p.jpg", _jpeg(color), "image/jpeg")})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _item_ids(client, name: str, item_type: str, query: str = "") -> list[int]:
    r = client.get(f"/api/projects/{name}{query}")
    assert r.status_code == 200, r.text
    return [i[item_type]["id"] for i in r.json()["items"] if i["item_type"] == item_type]


def _set_settings(client, name: str) -> None:
    """Trip settings away from their defaults."""
    assert client.put(f"/api/projects/{name}/track-style", json={
        "track_color": "#123456", "track_width": 4.5, "color_by_type": True,
        "type_styles": {"ride": {"color": "#ABCDEF", "style": "dashed"}},
    }).status_code == 204
    assert client.put(f"/api/projects/{name}/languages",
                      json={"languages": ["fr", "de"]}).status_code == 204


def _zip(client, name: str, query: str = "") -> zipfile.ZipFile:
    r = client.get(f"/api/projects/{name}/export-zip{query}")
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


def _trip_file(zf: zipfile.ZipFile) -> bytes:
    (name,) = [n for n in zf.namelist() if n.endswith(ProjectIO.EXTENSION)]
    return zf.read(name)


def _without_photo_refs(doc: dict) -> dict:
    for item in doc["items"]:
        content = item.get(item["item_type"])
        if isinstance(content, dict):
            content.pop("photo_refs", None)
    return doc


def _without_photos(doc: dict) -> dict:
    for item in doc["items"]:
        content = item.get(item["item_type"])
        if isinstance(content, dict):
            content.pop("photos", None)
    return doc


def test_the_zip_s_trip_file_is_the_full_trip_export_with_every_photo(env):
    client, _, ids, _, data = env
    assert _import(client, "Alps", _alps(2, 100)).status_code == 201
    _set_settings(client, "Alps")
    (mid,) = _item_ids(client, "Alps", "memory")
    (jid,) = _item_ids(client, "Alps", "journal")
    mem_photos = [_upload(client, "memories", mid, (200, 30, 30)),
                  _upload(client, "memories", mid, (30, 200, 30))]
    jnl_photos = [_upload(client, "journal", jid, (30, 30, 200))]

    traxj = client.get("/api/projects/Alps/export-traxj")
    assert traxj.status_code == 200, traxj.text
    zf = _zip(client, "Alps")
    doc = json.loads(_trip_file(zf))

    # Everything a trip file holds is there, not only items and activities.
    assert doc["people"] and doc["groups"] and doc["day_meta"]
    assert doc["sleeping_options"] and doc["languages"] == ["fr", "de"]
    assert doc["track_color"] == "#123456"
    # photo_refs name each photo where the archive has it.
    (mem,) = [i["memory"] for i in doc["items"] if i["item_type"] == "memory"]
    (jnl,) = [i["journal"] for i in doc["items"] if i["item_type"] == "journal"]
    assert mem["photos"] == mem_photos
    assert mem["photo_refs"] == [f"photos/{mid}/{u}.jpg" for u in mem["photos"]]
    assert jnl["photos"] == jnl_photos
    assert jnl["photo_refs"] == [f"journal/{jid}/{u}.jpg" for u in jnl_photos]
    # Apart from them, it is the .traxj export.
    assert _without_photo_refs(doc) == json.loads(traxj.content)

    # Every photo that exists on disk is in the archive, byte for byte.
    owner = data / "users" / str(ids["owner"])
    for uuid in mem_photos:
        assert zf.read(f"photos/{mid}/{uuid}.jpg") == \
            (owner / "memories" / str(mid) / f"{uuid}.jpg").read_bytes()
    for uuid in jnl_photos:
        assert zf.read(f"journal/{jid}/{uuid}.jpg") == \
            (owner / "journal" / str(jid) / f"{uuid}.jpg").read_bytes()
    # ...and nothing else besides the trip file: no thumbnails.
    assert sorted(n for n in zf.namelist() if not n.endswith(ProjectIO.EXTENSION)) == \
        sorted([f"photos/{mid}/{u}.jpg" for u in mem_photos]
               + [f"journal/{jid}/{u}.jpg" for u in jnl_photos])

    # And the trip file imports back to the same content, but for the photo
    # names: a .traxj carries no photo files, so its import keeps no name
    # (#469 Decision 9).
    assert _import(client, "Restored", _trip_file(zf)).status_code == 201
    restored = client.get("/api/projects/Restored/export-traxj").content
    for item in json.loads(restored)["items"]:
        if item["item_type"] in ("memory", "journal"):
            assert item[item["item_type"]]["photos"] == []
    assert _without_photos(_content(restored)) == _without_photos(_content(traxj.content))


def test_a_photo_missing_on_disk_is_left_out_but_still_referenced(env):
    client, _, ids, _, data = env
    assert _import(client, "Alps", _alps(1, 20)).status_code == 201
    (jid,) = _item_ids(client, "Alps", "journal")
    kept, lost = (_upload(client, "journal", jid, (1, 2, 3)),
                  _upload(client, "journal", jid, (4, 5, 6)))
    (data / "users" / str(ids["owner"]) / "journal" / str(jid) / f"{lost}.jpg").unlink()

    zf = _zip(client, "Alps")

    assert f"journal/{jid}/{kept}.jpg" in zf.namelist()
    assert f"journal/{jid}/{lost}.jpg" not in zf.namelist()
    (jnl,) = [i["journal"] for i in json.loads(_trip_file(zf))["items"]
              if i["item_type"] == "journal"]
    assert jnl["photo_refs"] == [f"journal/{jid}/{u}.jpg" for u in (kept, lost)]


def test_a_companion_s_zip_holds_the_trip_s_memory_photos_and_only_their_own_journal(env):
    """Memory photos come from the owner's folder; journal photos only from
    the caller's own entries, in the caller's folder."""
    client, engine, ids, act_as, data = env
    assert _import(client, "Alps", _alps(1, 20)).status_code == 201
    (mid,) = _item_ids(client, "Alps", "memory")
    (owner_jid,) = _item_ids(client, "Alps", "journal")
    mem_photo = _upload(client, "memories", mid, (9, 9, 9))
    owner_jnl_photo = _upload(client, "journal", owner_jid, (8, 8, 8))
    with Session(engine) as sess:
        project = sess.exec(select(DBProject).where(DBProject.name == "Alps")).one()
        sess.add(DBProjectMember(project_id=project.id, user_info_id=ids["companion"],
                                 role="editor", invited_by=ids["owner"], created_at=0.0))
        sess.commit()
    query = f"?owner={ids['owner']}"
    act_as("companion")
    r = client.post(f"/api/journal/{query}", json={
        "project_name": "Alps", "date": "2024-06-01", "geo_mode": "custom",
        "lat": 1.0, "lon": 2.0, "description": "mine"})
    assert r.status_code == 201, r.text
    comp_jid = r.json()["id"]
    comp_jnl_photo = _upload(client, "journal", comp_jid, (7, 7, 7))

    zf = _zip(client, "Alps", query)

    names = set(zf.namelist())
    assert f"photos/{mid}/{mem_photo}.jpg" in names
    assert f"journal/{comp_jid}/{comp_jnl_photo}.jpg" in names
    assert not any(n.startswith(f"journal/{owner_jid}/") for n in names)
    assert zf.read(f"journal/{comp_jid}/{comp_jnl_photo}.jpg") == (
        data / "users" / str(ids["companion"]) / "journal" / str(comp_jid)
        / f"{comp_jnl_photo}.jpg").read_bytes()
    journal_ids = [i["journal"]["id"] for i in json.loads(_trip_file(zf))["items"]
                   if i["item_type"] == "journal"]
    assert journal_ids == [comp_jid]
    assert owner_jnl_photo not in _trip_file(zf).decode()
