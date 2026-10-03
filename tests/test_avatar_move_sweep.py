"""The startup sweep that moves companion-uploaded avatars to the trip owner's
folder (issue #470).

Before #470 an avatar lived in the uploader's folder and counted against the
uploader. The sweep moves it, with its usage, and must be safe to interrupt
and to run again.
"""
from __future__ import annotations

import logging
import uuid as uuid_lib

import pytest
from sqlmodel import Session, SQLModel, select

import api.people as people_mod
import models.db as db_module
import src.people.avatar_move as avatar_move
from models.billing import UserUsage
from models.project_db import DBPerson, DBProject, DBProjectMember
from models.user import UserInfo

FULL = b"\xff\xd8full-size-avatar" * 50
THUMB = b"\xff\xd8thumb" * 10


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'sweep.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(people_mod, "_DATA_DIR", str(data_dir))

    with Session(engine) as sess:
        users = {role: UserInfo(display_name=role, email=f"{role}@e.com")
                 for role in ("owner", "editor", "viewer")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {role: u.id for role, u in users.items()}
        proj = DBProject(user_info_id=ids["owner"], name="Trip")
        sess.add(proj); sess.commit(); sess.refresh(proj)
        for role in ("editor", "viewer"):
            sess.add(DBProjectMember(project_id=proj.id, user_info_id=ids[role],
                                     role=role, invited_by=ids["owner"]))
        name = str(uuid_lib.uuid4())
        person = DBPerson(project_id=proj.id, name="Alice", avatar_photo=name)
        sess.add(person); sess.commit(); sess.refresh(person)
        ids["person"] = person.id
        ids["project"] = proj.id
        ids["name"] = name
    try:
        yield engine, ids, data_dir
    finally:
        engine.dispose()


def _folder(data_dir, uid, person_id):
    return data_dir / "users" / str(uid) / "people" / str(person_id)


def _put(data_dir, uid, person_id, name, full=FULL, thumb=THUMB):
    folder = _folder(data_dir, uid, person_id)
    folder.mkdir(parents=True, exist_ok=True)
    if full is not None:
        (folder / f"{name}.jpg").write_bytes(full)
    if thumb is not None:
        (folder / f"{name}_thumb.jpg").write_bytes(thumb)


def _set_usage(engine, uid, value):
    with Session(engine) as sess:
        sess.add(UserUsage(user_info_id=uid, storage_bytes=value))
        sess.commit()


def _usage(engine, uid) -> int:
    with Session(engine) as sess:
        row = sess.exec(select(UserUsage).where(UserUsage.user_info_id == uid)).first()
        return row.storage_bytes if row else 0


def _files(data_dir) -> list:
    return sorted(p.relative_to(data_dir).as_posix()
                  for p in data_dir.rglob("*") if p.is_file()) if data_dir.exists() else []


def _owner_files(ids):
    folder = f"users/{ids['owner']}/people/{ids['person']}"
    return [f"{folder}/{ids['name']}.jpg", f"{folder}/{ids['name']}_thumb.jpg"]


def test_member_avatar_moves_with_both_files_and_usage(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _set_usage(engine, ids["editor"], 10_000)
    _set_usage(engine, ids["owner"], 500)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 1

    assert _files(data_dir) == _owner_files(ids)
    owner_folder = _folder(data_dir, ids["owner"], ids["person"])
    assert (owner_folder / f"{ids['name']}.jpg").read_bytes() == FULL
    assert (owner_folder / f"{ids['name']}_thumb.jpg").read_bytes() == THUMB
    moved = len(FULL) + len(THUMB)
    assert _usage(engine, ids["editor"]) == 10_000 - moved
    assert _usage(engine, ids["owner"]) == 500 + moved
    assert (f"avatar moved person_id={ids['person']} from_user={ids['editor']} "
            f"to_user={ids['owner']}") in caplog.text
    assert "avatar move sweep: moved=1" in caplog.text

    # The avatar is now served from the owner's folder to every member.
    assert people_mod.photo_file(owner_folder, ids["name"]).is_file()


def test_second_run_changes_nothing(env):
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _set_usage(engine, ids["editor"], 10_000)
    assert avatar_move.move_companion_avatars() == 1
    files, editor, owner = _files(data_dir), _usage(engine, ids["editor"]), _usage(engine, ids["owner"])

    assert avatar_move.move_companion_avatars() == 0

    assert _files(data_dir) == files
    assert _usage(engine, ids["editor"]) == editor
    assert _usage(engine, ids["owner"]) == owner


@pytest.mark.parametrize("owner_has_thumb", [True, False])
def test_interrupted_move_finishes_without_double_counting(env, owner_has_thumb):
    """A run died after writing the owner's copy (all of it, or just the
    full-size file) and before deleting the member's: nothing was counted yet,
    so the next run counts the move exactly once."""
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _put(data_dir, ids["owner"], ids["person"], ids["name"],
         thumb=THUMB if owner_has_thumb else None)
    _set_usage(engine, ids["editor"], 10_000)
    _set_usage(engine, ids["owner"], 500)

    assert avatar_move.move_companion_avatars() == 1

    assert _files(data_dir) == _owner_files(ids)
    moved = len(FULL) + len(THUMB)
    assert _usage(engine, ids["editor"]) == 10_000 - moved
    assert _usage(engine, ids["owner"]) == 500 + moved

    assert avatar_move.move_companion_avatars() == 0
    assert _usage(engine, ids["owner"]) == 500 + moved


def test_interrupted_after_thumb_deleted_counts_only_the_rest(env):
    """A run died after deleting (and counting) the member's thumbnail: the
    next run moves only the full-size file's bytes."""
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"], thumb=None)
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _set_usage(engine, ids["editor"], 10_000)
    _set_usage(engine, ids["owner"], 500)

    assert avatar_move.move_companion_avatars() == 1

    assert _files(data_dir) == _owner_files(ids)
    assert _usage(engine, ids["editor"]) == 10_000 - len(FULL)
    assert _usage(engine, ids["owner"]) == 500 + len(FULL)


def test_avatar_found_nowhere_warns_and_sweep_continues(env, caplog):
    engine, ids, data_dir = env
    with Session(engine) as sess:
        lost = DBPerson(project_id=ids["project"], name="Lost",
                        avatar_photo=str(uuid_lib.uuid4()))
        sess.add(lost); sess.commit(); sess.refresh(lost)
        lost_id = lost.id
    _put(data_dir, ids["viewer"], ids["person"], ids["name"])

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 1

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "avatar not found" in r.getMessage()]
    assert len(warnings) == 1
    assert f"person_id={lost_id}" in warnings[0].getMessage()
    assert _files(data_dir) == _owner_files(ids)
    assert "not_found=1" in caplog.text


def test_owner_avatar_already_in_place_is_left_alone(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _set_usage(engine, ids["owner"], 500)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 0

    assert _files(data_dir) == _owner_files(ids)
    assert _usage(engine, ids["owner"]) == 500
    assert "WARNING" not in [r.levelname for r in caplog.records]


def test_differing_copies_are_left_in_place(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _put(data_dir, ids["owner"], ids["person"], ids["name"], full=b"other bytes")
    _set_usage(engine, ids["editor"], 10_000)

    with caplog.at_level(logging.WARNING, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 0

    assert len(_files(data_dir)) == 4
    assert _usage(engine, ids["editor"]) == 10_000
    assert "avatar copies differ" in caplog.text


def test_found_in_two_members_folders_is_left_in_place(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _put(data_dir, ids["viewer"], ids["person"], ids["name"])

    with caplog.at_level(logging.WARNING, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 0

    assert len(_files(data_dir)) == 4
    assert "several members' folders" in caplog.text


def test_one_failing_avatar_does_not_stop_the_others(env, monkeypatch, caplog):
    engine, ids, data_dir = env
    with Session(engine) as sess:
        other = DBPerson(project_id=ids["project"], name="Bob",
                         avatar_photo=str(uuid_lib.uuid4()))
        sess.add(other); sess.commit(); sess.refresh(other)
        other_id, other_name = other.id, other.avatar_photo
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _put(data_dir, ids["editor"], other_id, other_name)

    real = avatar_move._copy_into_place
    def flaky(src, dst):
        if dst.parent.name == str(other_id):
            raise OSError("disk full")
        real(src, dst)
    monkeypatch.setattr(avatar_move, "_copy_into_place", flaky)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 1

    assert "failed=1" in caplog.text
    # The failed one is untouched in the member's folder, to retry next start.
    assert (_folder(data_dir, ids["editor"], other_id) / f"{other_name}.jpg").is_file()
    assert not list(_folder(data_dir, ids["owner"], other_id).glob("*.moving"))


def test_sweep_exception_does_not_break_startup(monkeypatch):
    """The lifespan runs the sweep, and a sweep that blows up is logged and
    swallowed: the API still starts."""
    import api.router as router
    from fastapi.testclient import TestClient

    class _Scheduler:
        def add_job(self, *a, **k): pass
        def add_listener(self, *a, **k): pass
        def start(self): pass
        def shutdown(self, *a, **k): pass

    monkeypatch.setattr(router, "_IS_API_PROCESS", True)
    monkeypatch.setattr(router.alembic_command, "upgrade", lambda *a, **k: None)
    monkeypatch.setattr(router, "_check_schema_contract", lambda: None)
    monkeypatch.setattr(router, "seed_admin", lambda: None)
    monkeypatch.setattr(router, "sweep_orphaned_jobs", lambda: 0)
    monkeypatch.setattr(router, "sweep_orphaned_poster_jobs", lambda: 0)
    monkeypatch.setattr(router, "_scheduler", _Scheduler())

    calls = []
    def boom():
        calls.append(1)
        raise RuntimeError("sweep exploded")
    monkeypatch.setattr(avatar_move, "_move_all", boom)

    with TestClient(router.app) as client:
        assert client.get("/api/version").status_code == 200
    assert calls == [1]
