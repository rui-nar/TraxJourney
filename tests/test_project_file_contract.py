"""The project file format as the API presents it: extension and export names.

A project file's extension (``ProjectIO.EXTENSION``) is user-visible in four
places — the import upload, the single-file export's download name, the file
inside the ZIP export, and the ``filename`` in the project listing. They must
agree with each other and with the constant, or a user downloads a backup the
import screen then refuses. Exercised through the real routers.

Exactly one test also pins the literal extension, so changing the file format's
name (issue #151) is a deliberate, visible edit here rather than a side effect.
"""

from __future__ import annotations

import io
import json
import re
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user
from api.project_transfer import router as project_transfer_router
from api.projects import router as projects_router
from models.project_db import DBProject
from models.user import UserInfo
from src.project.project_io import ProjectIO

_FILENAME = re.compile(r'filename="([^"]+)"')


@pytest.fixture
def client(monkeypatch, tmp_path):
    """One user, in-memory DB, project files and usage walks under tmp_path."""
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

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(projects_router)
    app.include_router(project_transfer_router)
    return TestClient(app)


def _project_file_bytes(name: str) -> bytes:
    return json.dumps({
        "version": 1,
        "name": name,
        "trip_start": None,
        "filter_state": {"start_date": None, "end_date": None, "activity_types": None},
        "items": [],
        "activities": [],
    }).encode("utf-8")


def _import(client, filename: str):
    return client.post(
        "/api/projects/import",
        files={"file": (filename, _project_file_bytes("ignored"), "application/json")},
    )


def _create(client, name: str) -> None:
    r = client.post("/api/projects/", json={"name": name})
    assert r.status_code == 201, r.text


def _listing(client) -> dict[str, dict]:
    r = client.get("/api/projects/")
    assert r.status_code == 200, r.text
    return {entry["name"]: entry for entry in r.json()}


def test_import_names_the_project_after_the_file_minus_extension(client):
    r = _import(client, f"x{ProjectIO.EXTENSION}")

    assert r.status_code == 201, r.text
    assert r.json()["name"] == "x"
    assert "x" in _listing(client)


def test_single_file_export_downloads_with_the_project_extension(client):
    _create(client, "Trip")

    r = client.get("/api/projects/Trip/export-traxj")

    assert r.status_code == 200, r.text
    match = _FILENAME.search(r.headers["content-disposition"])
    assert match, r.headers["content-disposition"]
    assert match.group(1) == f"Trip{ProjectIO.EXTENSION}"
    # The download must be importable as-is: same extension, same content shape.
    assert json.loads(r.content)["name"] == "Trip"


def test_zip_export_contains_the_project_file_with_the_project_extension(client):
    _create(client, "Trip")

    r = client.get("/api/projects/Trip/export-zip")

    assert r.status_code == 200, r.text
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    project_files = [n for n in names if not n.startswith("photos/")]
    assert project_files == [f"Trip{ProjectIO.EXTENSION}"], names


def test_listing_filename_is_name_plus_extension(client):
    _create(client, "Trip")

    entry = _listing(client)["Trip"]

    assert entry["filename"] == "Trip" + ProjectIO.EXTENSION


def test_exported_file_round_trips_through_import(client):
    """What export hands the user is exactly what import accepts."""
    _create(client, "Trip")
    exported = client.get("/api/projects/Trip/export-traxj")
    download_name = _FILENAME.search(exported.headers["content-disposition"]).group(1)
    download_name = download_name.replace("Trip", "Restored", 1)

    r = client.post(
        "/api/projects/import",
        files={"file": (download_name, exported.content, "application/json")},
    )

    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Restored"


def test_project_file_extension_is_traxj(client):
    """The one literal pin. Renaming the format changes this line on purpose."""
    assert ProjectIO.EXTENSION == ".traxj"

    r = _import(client, "Summer.traxj")
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Summer"
    assert _listing(client)["Summer"]["filename"] == "Summer.traxj"


@pytest.mark.parametrize("bad_name", ["Summer.viewtrip", "Summer.gettracks", "Summer", ".traxj"])
def test_import_rejects_anything_but_a_traxj_file(client, bad_name):
    """Issue #151 dropped the ``.viewtrip``/``.gettracks`` formats with no
    compatibility. The upload is valid project JSON in every case, so only the
    extension check stands between an old file and a project named
    ``Summer.viewtrip``.
    """
    r = _import(client, bad_name)

    assert r.status_code == 400, r.text
    assert ".traxj" in r.json()["detail"]
    assert _listing(client) == {}


def test_old_export_route_is_gone_from_the_real_app(monkeypatch):
    """``/export-viewtrip`` was renamed to ``/export-traxj`` with no alias.

    Against the real app (no web build, so no SPA catch-all) and an existing
    project, so the 404 is the router's "no such route" — not "project not
    found", which is also a 404.
    """
    import api.router as router_mod

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        sess.add(DBProject(user_info_id=user.id, name="Trip"))
        sess.commit()
        uid = user.id

    app = router_mod.app
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    try:
        client = TestClient(app)
        assert client.get("/api/projects/Trip/export-traxj").status_code == 200

        r = client.get("/api/projects/Trip/export-viewtrip")

        assert r.status_code == 404
        assert r.json()["detail"] == "Not Found"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
