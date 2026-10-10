"""The owner-run cleanup of duplicate photos in Polarsteps memories (#566).

The script must keep the first copy of each content in a Polarsteps memory,
remove the others with their thumbnail and share copy, count the freed bytes
off the owner's storage, record the hashes of what it keeps, leave manual
memories and photos without a file alone, and find nothing to do on a second
run. It writes the row before deleting files, so an interruption leaves only
unlisted files, which the next run reports (review R1-2).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from models.billing import UserUsage
from models.project_db import DBMemory, DBProject
from models.user import UserInfo
from scripts import dedupe_memory_photos as dedupe

MISSING = "00000000-0000-4000-8000-000000000000"
FIRST = "11111111-1111-4111-8111-111111111111"
COPY_1 = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
COPY_2 = "44444444-4444-4444-8444-444444444444"
MANUAL_1 = "55555555-5555-4555-8555-555555555555"
MANUAL_2 = "66666666-6666-4666-8666-666666666666"
P2_FIRST = "77777777-7777-4777-8777-777777777777"
P2_COPY = "88888888-8888-4888-8888-888888888888"

SAME = b"the same photo, byte for byte" * 50
DIFFERENT = b"another photo" * 70
#: A hash OTHER already carries (a replaced photo carries its download's).
CARRIED = "c" * 64
INITIAL_USAGE = 1_000_000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _folder(data_dir: Path, memory_id: int) -> Path:
    return data_dir / "users" / "1" / "memories" / str(memory_id)


def _store(folder: Path, name: str, raw: bytes, share: bool = False) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.jpg").write_bytes(raw)
    (folder / f"{name}_thumb.jpg").write_bytes(b"thumb of " + name.encode())
    if share:
        (folder / f"{name}_share.jpg").write_bytes(b"share copy of " + name.encode())


@pytest.fixture
def engine(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)
    yield test_engine


@pytest.fixture
def data_dir(tmp_path, engine):
    """Project 1, memory 10 (Polarsteps): a listed photo with no file, then
    FIRST, its copy COPY_1, OTHER (different, carrying a hash already) and a
    second copy COPY_2 with a share copy. Memory 11 (manual) holds two copies.
    Project 2, memory 20 (Polarsteps): one photo and its copy."""
    f10, f11, f20 = _folder(tmp_path, 10), _folder(tmp_path, 11), _folder(tmp_path, 20)
    _store(f10, FIRST, SAME)
    _store(f10, COPY_1, SAME)
    _store(f10, OTHER, DIFFERENT)
    _store(f10, COPY_2, SAME, share=True)
    _store(f11, MANUAL_1, SAME)
    _store(f11, MANUAL_2, SAME)
    _store(f20, P2_FIRST, SAME)
    _store(f20, P2_COPY, SAME)
    with Session(engine) as sess:
        sess.add(UserInfo(id=1, email="u1@example.com", display_name="U1"))
        sess.add(DBProject(id=1, user_info_id=1, name="trip"))
        sess.add(DBProject(id=2, user_info_id=1, name="other trip"))
        sess.add(DBMemory(
            id=10, project_id=1, date="2026-01-01", polarsteps_step_id=100,
            photos_json=json.dumps([MISSING, FIRST, COPY_1, OTHER, COPY_2]),
            photo_order_json=json.dumps({
                "epoch": 3,
                "ranks": {FIRST: 0, COPY_1: 1, OTHER: 2, COPY_2: 3},
                "hashes": {OTHER: CARRIED, COPY_1: _sha(SAME)},
            }),
        ))
        sess.add(DBMemory(id=11, project_id=1, date="2026-01-02",
                          photos_json=json.dumps([MANUAL_1, MANUAL_2])))
        sess.add(DBMemory(id=20, project_id=2, date="2026-01-03", polarsteps_step_id=200,
                          photos_json=json.dumps([P2_FIRST, P2_COPY])))
        sess.add(UserUsage(user_info_id=1, storage_bytes=INITIAL_USAGE))
        sess.commit()
    return tmp_path


def _memory(engine, memory_id: int) -> tuple:
    with Session(engine) as sess:
        mem = sess.get(DBMemory, memory_id)
        return json.loads(mem.photos_json), mem.photo_order_json


def _usage(engine) -> int:
    with Session(engine) as sess:
        return sess.exec(select(UserUsage).where(UserUsage.user_info_id == 1)).one().storage_bytes


def _snapshot(data_dir: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(data_dir.rglob("*")) if p.is_file()}


def _rows(engine) -> dict:
    return {mid: _memory(engine, mid) for mid in (10, 11, 20)}


def _counted(folder: Path, *names: str) -> int:
    """What the usage counter holds for *names*: originals and thumbnails."""
    return sum((folder / f"{n}{s}.jpg").stat().st_size for n in names for s in ("", "_thumb"))


def _run(*argv) -> int:
    return dedupe.main(list(argv))


def _apply(data_dir: Path, *extra) -> int:
    return _run("--data-dir", str(data_dir), "--apply", "--api-stopped", *extra)


def test_apply_keeps_the_first_copy_and_removes_the_others(data_dir, engine, capsys):
    assert _apply(data_dir) == 0
    out = capsys.readouterr().out
    photos, raw_state = _memory(engine, 10)
    assert photos == [MISSING, FIRST, OTHER]
    state = json.loads(raw_state)
    assert state["epoch"] == 3
    assert state["ranks"] == {FIRST: 0, OTHER: 2}
    f10 = _folder(data_dir, 10)
    assert sorted(p.name for p in f10.iterdir()) == sorted(
        [f"{FIRST}.jpg", f"{FIRST}_thumb.jpg", f"{OTHER}.jpg", f"{OTHER}_thumb.jpg"])
    assert f"removed: {COPY_1} (same as {FIRST})" in out
    assert f"removed: {COPY_2} (same as {FIRST})" in out


def test_kept_photos_get_their_hashes_and_a_carried_hash_stays(data_dir, engine):
    assert _apply(data_dir) == 0
    state = json.loads(_memory(engine, 10)[1])
    assert state["hashes"] == {FIRST: _sha(SAME), OTHER: CARRIED}


def test_storage_usage_drops_by_the_removed_files_and_the_share_copy_goes(data_dir, engine,
                                                                         capsys):
    f10, f20 = _folder(data_dir, 10), _folder(data_dir, 20)
    removed = _counted(f10, COPY_1, COPY_2) + _counted(f20, P2_COPY)
    assert _apply(data_dir) == 0
    assert _usage(engine) == INITIAL_USAGE - removed
    assert not (f10 / f"{COPY_2}_share.jpg").exists()
    assert (f"memories touched 2 / photos removed 3 / bytes freed {removed} / "
            "hashes recorded 2 / missing files 1 / unlisted files 0") in capsys.readouterr().out


def test_manual_memories_are_untouched(data_dir, engine):
    before = _snapshot(_folder(data_dir, 11))
    row = _memory(engine, 11)
    assert _apply(data_dir) == 0
    assert _snapshot(_folder(data_dir, 11)) == before
    assert _memory(engine, 11) == row


def test_project_narrows_the_run(data_dir, engine):
    before = _snapshot(_folder(data_dir, 10))
    row = _memory(engine, 10)
    assert _apply(data_dir, "--project", "2") == 0
    assert _memory(engine, 20)[0] == [P2_FIRST]
    assert _snapshot(_folder(data_dir, 10)) == before
    assert _memory(engine, 10) == row


def test_a_missing_file_is_reported_and_never_a_duplicate(data_dir, engine, capsys):
    assert _apply(data_dir) == 0
    out = capsys.readouterr().out
    photos, raw_state = _memory(engine, 10)
    assert MISSING in photos
    assert MISSING not in json.loads(raw_state)["hashes"]
    assert f"memory 10: file missing for {MISSING}, never counted as a duplicate" in out


def test_dry_run_reports_and_writes_nothing(data_dir, engine, capsys):
    files, rows = _snapshot(data_dir), _rows(engine)
    assert _run("--data-dir", str(data_dir)) == 0
    out = capsys.readouterr().out
    assert _snapshot(data_dir) == files
    assert _rows(engine) == rows
    assert _usage(engine) == INITIAL_USAGE
    assert f"to remove: {COPY_1} (same as {FIRST})" in out
    assert "memories touched 2 / photos to remove 3" in out
    assert "Dry run: nothing written" in out


def test_apply_without_api_stopped_refuses_and_writes_nothing(data_dir, engine, capsys):
    files, rows = _snapshot(data_dir), _rows(engine)
    assert _run("--data-dir", str(data_dir), "--apply") != 0
    assert "Stop the API" in capsys.readouterr().err
    assert _snapshot(data_dir) == files
    assert _rows(engine) == rows
    assert _usage(engine) == INITIAL_USAGE


def test_second_run_is_a_no_op(data_dir, engine, capsys):
    assert _apply(data_dir) == 0
    capsys.readouterr()
    files, rows, usage = _snapshot(data_dir), _rows(engine), _usage(engine)
    assert _apply(data_dir) == 0
    out = capsys.readouterr().out
    assert _snapshot(data_dir) == files
    assert _rows(engine) == rows
    assert _usage(engine) == usage
    assert ("memories touched 0 / photos removed 0 / bytes freed 0 / hashes recorded 0 / "
            "missing files 1 / unlisted files 0") in out


def test_an_interruption_after_the_row_write_leaves_only_unlisted_files(data_dir, engine,
                                                                       capsys, monkeypatch):
    """Killed between the commit and the unlink: every listed photo still has
    its file, and the next run reports what was left (never deletes it)."""
    def killed(*_args, **_kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as m, pytest.raises(KeyboardInterrupt):
        m.setattr(dedupe, "unlink_and_record", killed)
        _apply(data_dir)
    capsys.readouterr()

    f10 = _folder(data_dir, 10)
    photos, _ = _memory(engine, 10)
    assert photos == [MISSING, FIRST, OTHER]
    for uuid in photos[1:]:
        assert (f10 / f"{uuid}.jpg").is_file() and (f10 / f"{uuid}_thumb.jpg").is_file()

    leftovers = sorted(f10 / f"{uuid}{s}" for uuid in (COPY_1, COPY_2)
                       for s in (".jpg", "_thumb.jpg")) + [f10 / f"{COPY_2}_share.jpg"]
    files = _snapshot(data_dir)
    assert _apply(data_dir) == 0
    out = capsys.readouterr().out
    for path in leftovers:
        assert f"unlisted file (not deleted): {path}" in out
        assert path.is_file()
    assert "unlisted files 5" in out
    # Memory 20 was never reached by the killed run; the re-run cleans it.
    assert _memory(engine, 20)[0] == [P2_FIRST]
    assert all(files[p] == p.read_bytes() for p in leftovers)
