"""People avatars live in the trip owner's folder (issue #470).

Every member of a trip — owner, editor or viewer companion — must resolve one
avatar folder, the owner's, and the owner's storage pays for it. An upload the
server refuses (no access, too large) must leave no file and no usage behind.
"""
from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, SQLModel, select

import api.people as people_mod
import models.db as db_module
from api.deps import get_current_user
from api.people import router as people_router
from models.billing import UserUsage
from models.project_db import DBPerson, DBProject, DBProjectMember
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'people.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(people_mod, "_DATA_DIR", str(data_dir))

    with Session(engine) as sess:
        users = {
            role: UserInfo(display_name=role, email=f"{role}@e.com")
            for role in ("owner", "editor", "viewer", "outsider")
        }
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {role: u.id for role, u in users.items()}
        proj = DBProject(user_info_id=ids["owner"], name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        for role in ("editor", "viewer"):
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=ids[role],
                                     role=role, invited_by=ids["owner"]))
        person = DBPerson(project_id=proj.id, name="Alice")
        sess.add(person); sess.commit(); sess.refresh(person)
        ids["person"] = person.id

    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(people_router)

    def act_as(who: str):
        current["uid"] = ids[who]

    try:
        yield TestClient(app), engine, ids, act_as, data_dir
    finally:
        engine.dispose()


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _upload(client, person_id, raw=None):
    return client.post(f"/api/people/{person_id}/avatar",
                       files={"file": ("a.jpg", raw or _jpeg_bytes(), "image/jpeg")})


def _usage(engine, uid: int) -> int:
    with Session(engine) as sess:
        row = sess.exec(select(UserUsage).where(UserUsage.user_info_id == uid)).first()
        return row.storage_bytes if row else 0


def _files(data_dir) -> list:
    return sorted(p.relative_to(data_dir).as_posix()
                  for p in data_dir.rglob("*") if p.is_file()) if data_dir.exists() else []


def _avatar_uuid(engine, person_id: int) -> str | None:
    with Session(engine) as sess:
        return sess.get(DBPerson, person_id).avatar_photo


def test_editor_upload_lands_in_owner_folder_and_counts_for_owner(env):
    client, engine, ids, act_as, data_dir = env
    act_as("editor")
    assert _upload(client, ids["person"]).status_code == 201

    uuid_str = _avatar_uuid(engine, ids["person"])
    folder = f"users/{ids['owner']}/people/{ids['person']}"
    assert _files(data_dir) == [f"{folder}/{uuid_str}.jpg", f"{folder}/{uuid_str}_thumb.jpg"]
    assert _usage(engine, ids["owner"]) > 0
    assert _usage(engine, ids["editor"]) == 0


def test_viewer_gets_owner_uploaded_avatar(env):
    client, engine, ids, act_as, data_dir = env
    assert _upload(client, ids["person"]).status_code == 201

    act_as("viewer")
    assert client.get(f"/api/people/{ids['person']}/avatar").status_code == 200
    assert client.get(f"/api/people/{ids['person']}/avatar/thumb").status_code == 200


def test_viewer_gets_editor_uploaded_avatar(env):
    client, engine, ids, act_as, data_dir = env
    act_as("editor")
    assert _upload(client, ids["person"]).status_code == 201

    for who in ("viewer", "owner"):
        act_as(who)
        assert client.get(f"/api/people/{ids['person']}/avatar").status_code == 200
        assert client.get(f"/api/people/{ids['person']}/avatar/thumb").status_code == 200


def test_viewer_cannot_upload(env):
    client, engine, ids, act_as, data_dir = env
    act_as("viewer")
    assert _upload(client, ids["person"]).status_code in (403, 404)
    assert _files(data_dir) == []
    assert _usage(engine, ids["viewer"]) == 0
    assert _usage(engine, ids["owner"]) == 0


def test_outsider_upload_writes_nothing(env):
    client, engine, ids, act_as, data_dir = env
    act_as("outsider")
    assert _upload(client, ids["person"]).status_code in (403, 404)
    assert _files(data_dir) == []
    assert _usage(engine, ids["outsider"]) == 0
    assert _usage(engine, ids["owner"]) == 0
    assert _avatar_uuid(engine, ids["person"]) is None


def test_upload_to_missing_person_writes_nothing(env):
    client, engine, ids, act_as, data_dir = env
    assert _upload(client, 999_999).status_code == 404
    assert _files(data_dir) == []
    assert _usage(engine, ids["owner"]) == 0


def test_oversized_upload_is_413_and_writes_nothing(env, monkeypatch):
    client, engine, ids, act_as, data_dir = env
    raw = _jpeg_bytes()
    # raising=False so the test runs (and fails on 201) against code without the cap.
    monkeypatch.setattr(people_mod, "_MAX_AVATAR_UPLOAD_BYTES", len(raw) - 1, raising=False)
    assert _upload(client, ids["person"], raw).status_code == 413
    assert _files(data_dir) == []
    assert _usage(engine, ids["owner"]) == 0
    assert _avatar_uuid(engine, ids["person"]) is None


def test_invalid_image_writes_nothing(env):
    client, engine, ids, act_as, data_dir = env
    assert _upload(client, ids["person"], b"not an image").status_code == 422
    assert _files(data_dir) == []
    assert _usage(engine, ids["owner"]) == 0


def test_replacing_avatar_removes_old_files_from_owner_folder(env):
    client, engine, ids, act_as, data_dir = env
    act_as("editor")
    assert _upload(client, ids["person"]).status_code == 201
    first_usage = _usage(engine, ids["owner"])
    assert _upload(client, ids["person"]).status_code == 201

    uuid_str = _avatar_uuid(engine, ids["person"])
    folder = f"users/{ids['owner']}/people/{ids['person']}"
    assert _files(data_dir) == [f"{folder}/{uuid_str}.jpg", f"{folder}/{uuid_str}_thumb.jpg"]
    assert _usage(engine, ids["owner"]) == first_usage


def test_editor_delete_avatar_removes_owner_folder_files(env):
    client, engine, ids, act_as, data_dir = env
    assert _upload(client, ids["person"]).status_code == 201

    act_as("editor")
    assert client.delete(f"/api/people/{ids['person']}/avatar").status_code == 204
    assert _files(data_dir) == []
    assert _usage(engine, ids["owner"]) == 0


def test_delete_person_removes_owner_folder_files(env):
    client, engine, ids, act_as, data_dir = env
    act_as("editor")
    assert _upload(client, ids["person"]).status_code == 201
    assert _files(data_dir) != []

    # A different member than the uploader deletes: both must resolve the
    # owner's folder.
    act_as("owner")
    assert client.delete(f"/api/people/{ids['person']}").status_code == 204
    assert _files(data_dir) == []
    assert _usage(engine, ids["owner"]) == 0


def test_serve_and_delete_create_no_folder(env):
    client, engine, ids, act_as, data_dir = env
    with Session(engine) as sess:
        row = sess.get(DBPerson, ids["person"])
        row.avatar_photo = "0f0e0d0c-0b0a-4908-8706-050403020100"  # file never written
        sess.add(row); sess.commit()

    assert client.get(f"/api/people/{ids['person']}/avatar").status_code == 404
    assert client.get(f"/api/people/{ids['person']}/avatar/thumb").status_code == 404
    assert client.delete(f"/api/people/{ids['person']}/avatar").status_code == 204
    assert not data_dir.exists()


def test_person_deleted_during_upload_leaves_no_files(env, monkeypatch):
    client, engine, ids, act_as, data_dir = env
    real_save = people_mod._save_avatar_files

    def save_then_delete_person(*args):
        real_save(*args)
        with Session(engine) as sess:
            sess.delete(sess.get(DBPerson, ids["person"]))
            sess.commit()

    monkeypatch.setattr(people_mod, "_save_avatar_files", save_then_delete_person)
    assert _upload(client, ids["person"]).status_code == 404
    assert _files(data_dir) == []
    assert _usage(engine, ids["owner"]) == 0
