"""An import stores only the photo names that have a file (#469).

A ``.traxj`` lists each memory's and journal entry's photos by name but holds
no photo files, and those names used to be stored as they were: the imported
trip then named photos that did not exist. Now a row stores the file's own
list, kept to the names that have a file:

* staged for it by a ZIP import (placed into its folder after the commit), or
* already in its folder, for a row a Replace keeps.

A name the file no longer lists stays dropped. The ingest moves no file: it
returns what to place, from the attempt that committed, and
:func:`place_photos` moves the staged files once the rows exist.
:func:`already_present` answers beforehand which photos a Replace finds in
place, by Replace's own matching.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
import src.project.photo_placement as placement_mod
from api.deps import get_current_user
from models.project_db import DBJournalEntry, DBMemory, DBProject
from models.user import UserInfo
from src.project.photo_placement import already_present, place_photos
from src.project.project_io import ProjectIO
from src.project.project_repo import ProjectRepo
from src.project.repo_transfer import Placement
from src.project.staged_photos import StagedPhoto


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


A, B, C, D, X = (_uuid(n) for n in (0xA, 0xB, 0xC, 0xD, 0xF))


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real app, an owner and a companion, a file-backed DB."""
    import api.router as router

    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for mod in (project_shared_mod, storage_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(data_dir))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = [UserInfo(display_name=n, email=f"{n}@e.com") for n in ("owner", "companion")]
        for u in users:
            sess.add(u)
        sess.commit()
        owner, companion = (u.id for u in users)

    router.app.dependency_overrides[get_current_user] = lambda: {"sub": str(owner)}
    try:
        yield {"client": TestClient(router.app, raise_server_exceptions=False),
               "engine": engine, "owner": owner, "companion": companion,
               "data": data_dir, "staging": tmp_path / "staging"}
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _memory(public_id: str, photos=(), file_id: int | None = None) -> dict:
    return {"item_type": "memory", "memory": {
        "id": file_id, "public_id": public_id, "name": public_id, "date": "2024-06-01",
        "photos": list(photos), "geo_mode": "custom", "lat": 45.0, "lon": 6.0}}


def _journal(text: str, photos=(), file_id: int | None = None) -> dict:
    return {"item_type": "journal", "journal": {
        "id": file_id, "date": "2024-06-01", "description": text,
        "photos": list(photos), "geo_mode": "custom", "lat": 45.0, "lon": 6.0}}


def _doc(*items) -> bytes:
    return json.dumps({
        "version": 1, "name": "ignored",
        "filter_state": {"start_date": None, "end_date": None, "activity_types": None},
        "items": list(items),
    }).encode("utf-8")


def _import(client, name: str, content: bytes, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    return client.post(
        "/api/projects/import", params=params,
        files={"file": (f"{name}{ProjectIO.EXTENSION}", content, "application/json")})


def _trip_id(env, name: str = "Alps") -> int:
    with Session(env["engine"]) as sess:
        return sess.exec(select(DBProject.id).where(
            DBProject.user_info_id == env["owner"], DBProject.name == name)).one()


def _photos(env, model, name: str = "Alps") -> dict:
    """The trip's rows of *model*, by description (journal) or name (memory):
    their stored photo names."""
    key = "description" if model is DBJournalEntry else "name"
    with Session(env["engine"]) as sess:
        rows = sess.exec(select(model).where(model.project_id == _trip_id(env, name))).all()
        return {getattr(r, key): json.loads(r.photos_json or "[]") for r in rows}


def _row_id(env, model, **where) -> int:
    with Session(env["engine"]) as sess:
        q = select(model.id).where(model.project_id == _trip_id(env))
        for col, val in where.items():
            q = q.where(getattr(model, col) == val)
        return sess.exec(q).one()


def _folder(env, kind: str, row_id: int, user: str = "owner") -> Path:
    return env["data"] / "users" / str(env[user]) / kind / str(row_id)


def _on_disk(env, kind: str, row_id: int, *names: str, user: str = "owner") -> None:
    """Put *names*' files into the row's folder and store the names, as an
    upload does."""
    folder = _folder(env, kind, row_id, user)
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        for suffix in ("", "_thumb"):
            (folder / f"{name}{suffix}.jpg").write_bytes(b"p" * 100)
    model = DBMemory if kind == "memories" else DBJournalEntry
    with Session(env["engine"]) as sess:
        row = sess.get(model, row_id)
        row.photos_json = json.dumps(list(names))
        sess.add(row)
        sess.commit()


def _stage(env, *uuids: str) -> dict:
    """Staged files for *uuids*, as the ZIP reader leaves them."""
    staging = env["staging"]
    staging.mkdir(exist_ok=True)
    staged = {}
    for u in uuids:
        full, thumb = staging / f"{u}.jpg", staging / f"{u}_thumb.jpg"
        full.write_bytes(b"f" * 300)
        thumb.write_bytes(b"t" * 40)
        staged[u] = StagedPhoto(full=full, thumb=thumb, bytes=340)
    return staged


def _files_under_users(env) -> list:
    return sorted(p for p in (env["data"] / "users").rglob("*") if p.is_file()) \
        if (env["data"] / "users").exists() else []


def _repo_import(env, content: bytes, name: str = "Alps", **kw) -> str:
    with Session(env["engine"]) as sess:
        return ProjectRepo().import_project(
            sess, env["owner"], name, ProjectIO.from_bytes(content), **kw)


def _repo_replace(env, content: bytes, name: str = "Alps", **kw):
    with Session(env["engine"]) as sess:
        return ProjectRepo().replace_project(
            sess, env["owner"], name, ProjectIO.from_bytes(content),
            data_dir=str(env["data"]), **kw)


def _alps(env) -> tuple[int, int]:
    """The owner's trip "Alps": memory pub-lake with A and B on disk, and the
    owner's journal entry "note" with C and D on disk. Returns their ids."""
    assert _import(env["client"], "Alps", _doc(
        _memory("pub-lake"), _journal("note"))).status_code == 201
    lake = _row_id(env, DBMemory, public_id="pub-lake")
    note = _row_id(env, DBJournalEntry, description="note")
    _on_disk(env, "memories", lake, A, B)
    _on_disk(env, "journal", note, C, D)
    return lake, note


# ── A .traxj stores no name without a file ──────────────────────────────────

@pytest.mark.parametrize("on_conflict", [None, "copy"])
def test_a_traxj_import_stores_no_photo_name(env, on_conflict):
    doc = _doc(_memory("pub-lake", [A, B], file_id=5), _journal("note", [C], file_id=6))
    name = "Alps"
    if on_conflict == "copy":
        assert _import(env["client"], "Alps", _doc()).status_code == 201
        name = "Alps (2)"

    r = _import(env["client"], "Alps", doc, on_conflict=on_conflict)

    assert r.status_code == 201, r.text
    assert r.json()["name"] == name
    assert _photos(env, DBMemory, name) == {"pub-lake": []}
    assert _photos(env, DBJournalEntry, name) == {"note": []}


def test_a_traxj_replace_stores_no_photo_name_without_a_file(env):
    assert _import(env["client"], "Alps", _doc(
        _memory("pub-lake"), _journal("note"))).status_code == 201
    note = _row_id(env, DBJournalEntry, description="note")

    r = _import(env["client"], "Alps", _doc(
        _memory("pub-lake", [A]),              # kept, A not on disk
        _memory("pub-new", [B]),               # new
        _journal("note", [C], file_id=note),   # kept, C not on disk
        _journal("fresh", [D])), on_conflict="replace")  # new

    assert r.status_code == 201, r.text
    assert _photos(env, DBMemory) == {"pub-lake": [], "pub-new": []}
    assert _photos(env, DBJournalEntry) == {"note": [], "fresh": []}


def test_replace_keeps_the_names_on_disk_and_not_those_the_file_dropped(env):
    lake, note = _alps(env)

    r = _import(env["client"], "Alps", _doc(
        _memory("pub-lake", [B, X]), _journal("note", [D, X], file_id=note)),
        on_conflict="replace")

    assert r.status_code == 201, r.text
    # B and D are on disk; X never was; A and C the file no longer lists.
    assert _photos(env, DBMemory) == {"pub-lake": [B]}
    assert _photos(env, DBJournalEntry) == {"note": [D]}
    assert sorted(p.name for p in _folder(env, "memories", lake).iterdir()) == [
        f"{B}.jpg", f"{B}_thumb.jpg"]
    assert sorted(p.name for p in _folder(env, "journal", note).iterdir()) == [
        f"{D}.jpg", f"{D}_thumb.jpg"]


# ── Staged photos: named at ingest, placed after the commit ─────────────────

def test_an_import_stores_the_staged_names_and_moves_no_file(env):
    staged = {("memories", 5): _stage(env, A), ("journal", 6): _stage(env, B)}
    placements: list = []

    name = _repo_import(env, _doc(
        _memory("pub-lake", [A, X], file_id=5), _journal("note", [B], file_id=6)),
        staged=staged, placements=placements)

    assert name == "Alps"
    assert _photos(env, DBMemory) == {"pub-lake": [A]}
    assert _photos(env, DBJournalEntry) == {"note": [B]}
    lake = _row_id(env, DBMemory, public_id="pub-lake")
    note = _row_id(env, DBJournalEntry, description="note")
    assert [(p.kind, p.row_id, p.uuid, p.staged) for p in placements] == [
        ("memories", lake, A, staged[("memories", 5)][A]),
        ("journal", note, B, staged[("journal", 6)][B])]
    assert all(p.staged.full.exists() and p.staged.thumb.exists() for p in placements)
    assert _files_under_users(env) == []


def test_replace_does_not_place_a_photo_already_in_place(env):
    lake, note = _alps(env)
    staged = {("memories", 5): _stage(env, A, X), ("journal", 6): _stage(env, C)}
    placements: list = []

    _repo_replace(env, _doc(
        _memory("pub-lake", [A, B, X], file_id=5), _journal("note", [C, D], file_id=note),
        _journal("fresh", [C], file_id=6)),
        staged=staged, placements=placements)

    assert _photos(env, DBMemory) == {"pub-lake": [A, B, X]}
    assert _photos(env, DBJournalEntry)["note"] == [C, D]
    fresh = _row_id(env, DBJournalEntry, description="fresh")
    # A is in the kept memory's folder already; X is not. The kept entry
    # "note" has file id `note`, for which nothing is staged; "fresh" is new.
    assert [(p.kind, p.row_id, p.uuid) for p in placements] == [
        ("memories", lake, X), ("journal", fresh, C)]
    assert staged[("memories", 5)][A].full.exists()  # untouched, not placed


def test_a_retried_ingest_returns_every_staged_photo_and_moves_nothing(env, monkeypatch):
    staged = {("memories", 5): _stage(env, A, B), ("journal", 6): _stage(env, C)}
    placements: list = []
    attempts = []

    with Session(env["engine"]) as sess:
        commit = sess.commit

        def lose_the_race_once():
            attempts.append(_files_under_users(env))
            if len(attempts) == 1:
                raise IntegrityError("INSERT", {}, Exception(
                    "UNIQUE constraint failed: project.user_info_id, project.name"))
            commit()

        monkeypatch.setattr(sess, "commit", lose_the_race_once)
        name = ProjectRepo().import_project(
            sess, env["owner"], "Alps", ProjectIO.from_bytes(_doc(
                _memory("pub-lake", [A, B], file_id=5), _journal("note", [C], file_id=6))),
            copy=True, staged=staged, placements=placements)

    assert len(attempts) == 2
    assert attempts[0] == [] and _files_under_users(env) == []
    assert name == "Alps"
    lake = _row_id(env, DBMemory, public_id="pub-lake")
    note = _row_id(env, DBJournalEntry, description="note")
    assert [(p.kind, p.row_id, p.uuid) for p in placements] == [
        ("memories", lake, A), ("memories", lake, B), ("journal", note, C)]
    assert all(p.staged.full.exists() and p.staged.thumb.exists() for p in placements)


# ── place_photos ────────────────────────────────────────────────────────────

@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(placement_mod, "record_written",
                        lambda user, *paths: calls.append((user, paths)))
    return calls


def test_place_photos_moves_the_files_under_the_new_row_and_counts_them_once(env, recorded):
    staged = _stage(env, A, B)
    placements = [Placement("memories", 42, A, staged[A]),
                  Placement("journal", 43, B, staged[B])]

    failed = place_photos(env["data"], env["owner"], placements)

    assert failed == []
    placed = [_folder(env, "memories", 42) / f"{A}.jpg",
              _folder(env, "memories", 42) / f"{A}_thumb.jpg",
              _folder(env, "journal", 43) / f"{B}.jpg",
              _folder(env, "journal", 43) / f"{B}_thumb.jpg"]
    assert [p.read_bytes()[:1] for p in placed] == [b"f", b"t", b"f", b"t"]
    assert not any(s.full.exists() or s.thumb.exists() for s in staged.values())
    assert recorded == [(env["owner"], tuple(placed))]


def test_place_photos_returns_a_failed_rename_instead_of_raising(env, recorded, monkeypatch, caplog):
    staged = _stage(env, A, B)
    real = os.replace

    def failing(src, dst):
        if Path(src) == staged[A].full:
            raise OSError("disk error")
        real(src, dst)

    monkeypatch.setattr(os, "replace", failing)
    with caplog.at_level(logging.ERROR, logger=placement_mod.__name__):
        failed = place_photos(env["data"], env["owner"], [
            Placement("memories", 42, A, staged[A]), Placement("memories", 42, B, staged[B])])

    assert failed == [("memories", 42, A)]
    assert not (_folder(env, "memories", 42) / f"{A}.jpg").exists()
    assert (_folder(env, "memories", 42) / f"{B}.jpg").exists()
    assert recorded == [(env["owner"], (_folder(env, "memories", 42) / f"{B}.jpg",
                                        _folder(env, "memories", 42) / f"{B}_thumb.jpg"))]
    (rec,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "memories" in rec.getMessage() and "42" in rec.getMessage() and A in rec.getMessage()


def test_a_thumbnail_rename_failing_after_its_full_file_moved_is_logged(
        env, recorded, monkeypatch, caplog):
    """The R3-4 guard: the full file stays, counted and soon unnamed, and the
    log says which half was placed, where, and how big it is."""
    staged = _stage(env, A)
    real = os.replace

    def failing(src, dst):
        if Path(src) == staged[A].thumb:
            raise OSError("disk error")
        real(src, dst)

    monkeypatch.setattr(os, "replace", failing)
    with caplog.at_level(logging.ERROR, logger=placement_mod.__name__):
        failed = place_photos(env["data"], env["owner"], [
            Placement("journal", 43, A, staged[A])])

    full = _folder(env, "journal", 43) / f"{A}.jpg"
    assert failed == [("journal", 43, A)]
    assert full.exists()
    assert recorded == [(env["owner"], (full,))]
    (rec,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    message = rec.getMessage()
    for part in ("journal", "43", A, "thumbnail", "full file", str(full), "300 bytes"):
        assert part in message, part


# ── already_present ─────────────────────────────────────────────────────────

def test_already_present_is_empty_for_a_trip_that_does_not_exist(env):
    project = ProjectIO.from_bytes(_doc(_memory("pub-lake", [A], file_id=5)))
    with Session(env["engine"]) as sess:
        assert already_present(sess, env["owner"], "Nowhere", project,
                               data_dir=env["data"]) == set()


def test_already_present_matches_as_replace_does(env):
    lake, note = _alps(env)
    # A companion's entry, with a file in the owner's folder under its id: not
    # the owner's, so Replace does not keep it, and nothing of it is present.
    with Session(env["engine"]) as sess:
        theirs = DBJournalEntry(project_id=_trip_id(env), user_info_id=env["companion"],
                                date="2024-06-01", description="theirs")
        sess.add(theirs)
        sess.commit()
        theirs = theirs.id
    _on_disk(env, "journal", theirs, D)
    # Another trip's memory holds A too, under another public_id.
    assert _import(env["client"], "Other", _doc(_memory("pub-other"))).status_code == 201
    doc = _doc(
        _memory("pub-lake", [A, X], file_id=5),        # kept: A present, X not
        _memory("pub-lake", [B], file_id=7),           # same public_id again: new row
        _memory("pub-other", [A], file_id=8),          # another trip's: new row
        _journal("note", [C], file_id=note),           # the owner's, kept: C present
        _journal("theirs", [D], file_id=theirs),       # a companion's: new row
    )
    project = ProjectIO.from_bytes(doc)

    with Session(env["engine"]) as sess:
        present = already_present(sess, env["owner"], "Alps", project, data_dir=env["data"])

    assert present == {("memories", 5, A), ("journal", note, C)}

    # What Replace then places is exactly the staged photos not present.
    staged = {("memories", 5): _stage(env, A, X), ("memories", 7): _stage(env, B),
              ("memories", 8): {A: StagedPhoto(env["staging"] / "a8.jpg",
                                               env["staging"] / "a8_thumb.jpg", 1)},
              ("journal", note): _stage(env, C), ("journal", theirs): _stage(env, D)}
    placements: list = []
    _repo_replace(env, doc, staged=staged, placements=placements)
    assert sorted((p.kind, p.uuid) for p in placements) == sorted(
        (kind, u) for (kind, fid), photos in staged.items() for u in photos
        if (kind, fid, u) not in present)
    # Of the kept rows, only the memory gets a photo placed: X.
    assert [(p.kind, p.uuid) for p in placements if (p.kind, p.row_id) in {
        ("memories", lake), ("journal", note)}] == [("memories", X)]


def test_replace_without_a_data_dir_is_refused_and_changes_nothing(env):
    """Without the data root no kept row's photo is found on disk, so every
    kept row would silently lose its names (review U2R1-2)."""
    lake, note = _alps(env)
    with Session(env["engine"]) as sess:
        with pytest.raises(ValueError, match="replace_project"):
            ProjectRepo().replace_project(
                sess, env["owner"], "Alps", ProjectIO.from_bytes(_doc(
                    _memory("pub-lake", [A, B]), _journal("note", [C, D], file_id=note))))
    assert _photos(env, DBMemory) == {"pub-lake": [A, B]}
    assert _photos(env, DBJournalEntry) == {"note": [C, D]}
