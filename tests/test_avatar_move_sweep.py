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


# ── Guard U5-R1-1: an owner-side path photo_file refuses ─────────────────────

def test_refused_owner_path_fails_before_any_delete(env, monkeypatch, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _set_usage(engine, ids["editor"], 10_000)
    owner_folder = _folder(data_dir, ids["owner"], ids["person"])

    real = avatar_move.photo_file
    def refuse_owner_thumb(folder, name, suffix=""):
        if folder == owner_folder and suffix == "_thumb":
            return None
        return real(folder, name, suffix)
    monkeypatch.setattr(avatar_move, "photo_file", refuse_owner_thumb)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 0

    member_folder = _folder(data_dir, ids["editor"], ids["person"])
    assert (member_folder / f"{ids['name']}_thumb.jpg").read_bytes() == THUMB
    assert (member_folder / f"{ids['name']}.jpg").read_bytes() == FULL
    assert _usage(engine, ids["editor"]) == 10_000
    assert "avatar move failed" in caplog.text
    assert "failed=1" in caplog.text


# ── Guard U5-R1-2: replacing an avatar the owner's folder doesn't hold ───────

def test_replacing_avatar_missing_from_owner_folder_warns(env, caplog):
    import io

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image

    from api.deps import get_current_user

    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(ids["owner"])}
    app.include_router(people_mod.router)
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 200, 30)).save(buf, "JPEG")

    with caplog.at_level(logging.WARNING, logger=people_mod.__name__):
        r = TestClient(app).post(f"/api/people/{ids['person']}/avatar",
                                 files={"file": ("a.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 201

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "old avatar not in owner folder" in r.getMessage()]
    assert len(warnings) == 1
    assert f"person_id={ids['person']}" in warnings[0]
    assert f"owner_dir={ids['owner']}" in warnings[0]
    assert ids["name"] in warnings[0]


# ── Owner decision: pre-U4 leftovers in members' folders ─────────────────────

def test_stale_pair_in_member_folder_is_deleted_with_usage_given_back(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    stale = str(uuid_lib.uuid4())
    _put(data_dir, ids["editor"], ids["person"], stale)
    _set_usage(engine, ids["editor"], 10_000)
    _set_usage(engine, ids["owner"], 500)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == _owner_files(ids)
    assert _usage(engine, ids["editor"]) == 10_000 - len(FULL) - len(THUMB)
    assert _usage(engine, ids["owner"]) == 500
    assert caplog.text.count("stale avatar file deleted") == 2
    assert "stale_deleted=2" in caplog.text


def test_stale_cleanup_also_covers_people_without_avatar(env):
    engine, ids, data_dir = env
    with Session(engine) as sess:
        bare = DBPerson(project_id=ids["project"], name="Bare")
        sess.add(bare); sess.commit(); sess.refresh(bare)
        bare_id = bare.id
    _put(data_dir, ids["viewer"], bare_id, str(uuid_lib.uuid4()))

    avatar_move.move_companion_avatars()

    assert not list(_folder(data_dir, ids["viewer"], bare_id).iterdir())


def test_pending_move_is_moved_not_deleted(env, monkeypatch):
    """A member's copy of a current avatar is untouched until it is moved."""
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _set_usage(engine, ids["editor"], 10_000)

    # The move fails this run: the member's copy must survive the cleanup.
    def broken(*a, **k):
        raise OSError("disk full")
    real = avatar_move._copy_into_place
    monkeypatch.setattr(avatar_move, "_copy_into_place", broken)
    avatar_move.move_companion_avatars()
    member_folder = _folder(data_dir, ids["editor"], ids["person"])
    assert sorted(p.name for p in member_folder.iterdir()) == \
        [f"{ids['name']}.jpg", f"{ids['name']}_thumb.jpg"]
    assert _usage(engine, ids["editor"]) == 10_000

    monkeypatch.setattr(avatar_move, "_copy_into_place", real)
    assert avatar_move.move_companion_avatars() == 1
    assert _files(data_dir) == _owner_files(ids)


def test_another_persons_current_avatar_is_never_deleted(env):
    """A name in this member's folder for one person that is another person's
    current avatar (any trip) is kept."""
    engine, ids, data_dir = env
    with Session(engine) as sess:
        other_owner = UserInfo(display_name="other", email="other@e.com")
        sess.add(other_owner); sess.commit(); sess.refresh(other_owner)
        other_proj = DBProject(user_info_id=other_owner.id, name="Elsewhere")
        sess.add(other_proj); sess.commit(); sess.refresh(other_proj)
        other_name = str(uuid_lib.uuid4())
        sess.add(DBPerson(project_id=other_proj.id, name="Carol", avatar_photo=other_name))
        sess.commit()
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _put(data_dir, ids["editor"], ids["person"], other_name)

    avatar_move.move_companion_avatars()

    folder = _folder(data_dir, ids["editor"], ids["person"])
    assert sorted(p.name for p in folder.iterdir()) == \
        [f"{other_name}.jpg", f"{other_name}_thumb.jpg"]


def test_owner_stale_pair_is_deleted_and_current_avatar_stays(env, caplog):
    """A companion replaced an avatar the owner uploaded before this release:
    the owner's old pair goes, with its usage; the current avatar stays."""
    engine, ids, data_dir = env
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _put(data_dir, ids["owner"], ids["person"], str(uuid_lib.uuid4()))
    _set_usage(engine, ids["owner"], 10_000)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == _owner_files(ids)
    assert _usage(engine, ids["owner"]) == 10_000 - len(FULL) - len(THUMB)
    assert caplog.text.count("stale avatar file deleted") == 2
    assert "owner_stale_deleted=2" in caplog.text


def test_non_photo_names_in_member_folder_are_left(env):
    engine, ids, data_dir = env
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    folder = _folder(data_dir, ids["editor"], ids["person"])
    folder.mkdir(parents=True)
    (folder / "notes.jpg").write_bytes(b"x")
    (folder / f"{uuid_lib.uuid4()}.png").write_bytes(b"x")

    avatar_move.move_companion_avatars()

    assert len(list(folder.iterdir())) == 2


def test_stale_cleanup_second_run_is_a_no_op(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _put(data_dir, ids["editor"], ids["person"], str(uuid_lib.uuid4()))
    _set_usage(engine, ids["editor"], 10_000)
    avatar_move.move_companion_avatars()
    files, editor = _files(data_dir), _usage(engine, ids["editor"])

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == files
    assert _usage(engine, ids["editor"]) == editor
    assert "stale_deleted=0" in caplog.text


# ── U5-R2-1: folders whose person row is gone ────────────────────────────────

def _delete_person(engine, person_id):
    with Session(engine) as sess:
        sess.delete(sess.get(DBPerson, person_id))
        sess.commit()


def test_companion_pair_of_deleted_person_is_deleted(env, caplog):
    engine, ids, data_dir = env
    stale = str(uuid_lib.uuid4())
    _put(data_dir, ids["editor"], ids["person"], stale)
    _delete_person(engine, ids["person"])
    _set_usage(engine, ids["editor"], 10_000)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == []
    assert not _folder(data_dir, ids["editor"], ids["person"]).exists()
    assert _usage(engine, ids["editor"]) == 10_000 - len(FULL) - len(THUMB)
    assert "orphan_deleted=2" in caplog.text


def test_pair_left_after_whole_trip_deleted_is_deleted(env):
    """The trip and its members are gone too: every user's tree is scanned,
    not only current members'."""
    engine, ids, data_dir = env
    stale = str(uuid_lib.uuid4())
    _put(data_dir, ids["editor"], ids["person"], stale)
    with Session(engine) as sess:
        for m in sess.exec(select(DBProjectMember)).all():
            sess.delete(m)
        sess.delete(sess.get(DBPerson, ids["person"]))
        sess.delete(sess.get(DBProject, ids["project"]))
        sess.commit()
    _set_usage(engine, ids["editor"], 10_000)

    avatar_move.move_companion_avatars()

    assert _files(data_dir) == []
    assert _usage(engine, ids["editor"]) == 10_000 - len(FULL) - len(THUMB)


def test_orphan_folder_keeps_non_photo_files_and_current_names(env):
    engine, ids, data_dir = env
    with Session(engine) as sess:
        other = DBPerson(project_id=ids["project"], name="Dan",
                         avatar_photo=str(uuid_lib.uuid4()))
        sess.add(other); sess.commit(); sess.refresh(other)
        other_name = other.avatar_photo
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    gone = 999_999
    _put(data_dir, ids["viewer"], gone, other_name)
    _put(data_dir, ids["viewer"], gone, str(uuid_lib.uuid4()))
    (_folder(data_dir, ids["viewer"], gone) / "notes.txt").write_bytes(b"x")

    avatar_move.move_companion_avatars()

    assert sorted(p.name for p in _folder(data_dir, ids["viewer"], gone).iterdir()) == \
        sorted([f"{other_name}.jpg", f"{other_name}_thumb.jpg", "notes.txt"])


def test_owner_folder_keeps_any_persons_current_avatar(env):
    """A name that is another person's current avatar is never deleted from
    the owner's folder, and the folder holding the current avatar stays."""
    engine, ids, data_dir = env
    with Session(engine) as sess:
        other = DBPerson(project_id=ids["project"], name="Eve",
                         avatar_photo=str(uuid_lib.uuid4()))
        sess.add(other); sess.commit(); sess.refresh(other)
        other_name = other.avatar_photo
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _put(data_dir, ids["owner"], ids["person"], other_name)
    _set_usage(engine, ids["owner"], 10_000)

    avatar_move.move_companion_avatars()

    assert len(_files(data_dir)) == 4
    assert _usage(engine, ids["owner"]) == 10_000


def test_avatar_moved_into_owner_folder_this_run_stays(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _put(data_dir, ids["owner"], ids["person"], str(uuid_lib.uuid4()))
    _set_usage(engine, ids["editor"], 10_000)
    _set_usage(engine, ids["owner"], 10_000)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 1

    assert _files(data_dir) == _owner_files(ids)
    # The owner gains the moved pair and loses the stale one: both are the
    # same size here.
    assert _usage(engine, ids["owner"]) == 10_000
    assert _usage(engine, ids["editor"]) == 10_000 - len(FULL) - len(THUMB)
    assert "owner_stale_deleted=2" in caplog.text


def test_owner_cleanup_second_run_is_a_no_op(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _put(data_dir, ids["owner"], ids["person"], str(uuid_lib.uuid4()))
    _set_usage(engine, ids["owner"], 10_000)
    avatar_move.move_companion_avatars()
    assert _files(data_dir) == _owner_files(ids)
    owner = _usage(engine, ids["owner"])

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == _owner_files(ids)
    assert _usage(engine, ids["owner"]) == owner
    assert "owner_stale_deleted=0" in caplog.text


def _ex_member(engine):
    """A user who was once a member of the trip: no membership row now."""
    with Session(engine) as sess:
        ex = UserInfo(display_name="ex", email="ex@e.com")
        sess.add(ex); sess.commit(); sess.refresh(ex)
        return ex.id


def test_ex_members_stale_pair_for_living_person_is_deleted(env, caplog):
    engine, ids, data_dir = env
    ex_id = _ex_member(engine)
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _put(data_dir, ex_id, ids["person"], str(uuid_lib.uuid4()))
    _set_usage(engine, ex_id, 10_000)
    _set_usage(engine, ids["owner"], 500)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == _owner_files(ids)
    assert not _folder(data_dir, ex_id, ids["person"]).exists()
    assert _usage(engine, ex_id) == 10_000 - len(FULL) - len(THUMB)
    assert _usage(engine, ids["owner"]) == 500
    assert caplog.text.count("stale avatar file deleted") == 2
    assert "orphan_deleted=2" in caplog.text


def test_ex_members_copy_of_current_avatar_is_kept(env, caplog):
    """The deferred R1-2 case: the sweep can't move it, and must not delete it."""
    engine, ids, data_dir = env
    ex_id = _ex_member(engine)
    _put(data_dir, ex_id, ids["person"], ids["name"])
    _set_usage(engine, ex_id, 10_000)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert sorted(p.name for p in _folder(data_dir, ex_id, ids["person"]).iterdir()) == \
        [f"{ids['name']}.jpg", f"{ids['name']}_thumb.jpg"]
    assert _usage(engine, ex_id) == 10_000
    assert "not_found=1" in caplog.text


def test_ex_member_cleanup_second_run_is_a_no_op(env, caplog):
    engine, ids, data_dir = env
    ex_id = _ex_member(engine)
    _put(data_dir, ids["owner"], ids["person"], ids["name"])
    _put(data_dir, ex_id, ids["person"], str(uuid_lib.uuid4()))
    _set_usage(engine, ex_id, 10_000)
    avatar_move.move_companion_avatars()
    assert not _folder(data_dir, ex_id, ids["person"]).exists()
    files, ex_usage = _files(data_dir), _usage(engine, ex_id)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == files
    assert _usage(engine, ex_id) == ex_usage
    assert "stale_deleted=0 orphan_deleted=0" in caplog.text


def test_trip_less_living_persons_folders_are_left_alone(env):
    """A person row whose trip is gone has no known owner: none of their
    folders are touched."""
    engine, ids, data_dir = env
    ex_id = _ex_member(engine)
    with Session(engine) as sess:
        stray = DBPerson(project_id=987_654, name="Stray")
        sess.add(stray); sess.commit(); sess.refresh(stray)
        stray_id = stray.id
    _put(data_dir, ex_id, stray_id, str(uuid_lib.uuid4()))

    avatar_move.move_companion_avatars()

    assert len(list(_folder(data_dir, ex_id, stray_id).iterdir())) == 2


def test_living_persons_member_folder_keeps_member_rules(env, caplog):
    """The pending move of a current avatar still happens, a stale name is
    still cleaned, and nothing is counted as orphaned."""
    engine, ids, data_dir = env
    stale = str(uuid_lib.uuid4())
    _put(data_dir, ids["editor"], ids["person"], ids["name"])
    _put(data_dir, ids["editor"], ids["person"], stale)

    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        assert avatar_move.move_companion_avatars() == 1

    assert _files(data_dir) == _owner_files(ids)
    assert "stale_deleted=2 orphan_deleted=0" in caplog.text


def test_orphan_cleanup_second_run_is_a_no_op(env, caplog):
    engine, ids, data_dir = env
    _put(data_dir, ids["editor"], ids["person"], str(uuid_lib.uuid4()))
    _delete_person(engine, ids["person"])
    _set_usage(engine, ids["editor"], 10_000)
    avatar_move.move_companion_avatars()
    files, editor = _files(data_dir), _usage(engine, ids["editor"])

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=avatar_move.__name__):
        avatar_move.move_companion_avatars()

    assert _files(data_dir) == files
    assert _usage(engine, ids["editor"]) == editor
    assert "orphan_deleted=0" in caplog.text
