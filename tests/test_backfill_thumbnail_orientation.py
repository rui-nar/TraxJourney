"""The one-off repair of sideways thumbnails stored before #511.

The script must rewrite only memory and journal thumbnails of photos with an
orientation tag, leave avatars, upright photos and unreadable originals alone,
keep counted storage equal to the files on disk, and find nothing to do on a
second run: the original keeps its tag, so only the pixels can tell.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageOps, ImageStat
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from models.billing import UserUsage
from models.user import UserInfo
from scripts import backfill_thumbnail_orientation as backfill
from src.admin.storage import dir_size
from src.utils.photo_store import write_photo_files
from tests.test_rail_data_fetch import _dockerignore_excludes

ROOT = Path(__file__).resolve().parent.parent

ROTATED_MEMORY = "11111111-1111-4111-8111-111111111111"
UPRIGHT_MEMORY = "22222222-2222-4222-8222-222222222222"
CORRUPT_MEMORY = "33333333-3333-4333-8333-333333333333"
ROTATED_JOURNAL = "44444444-4444-4444-8444-444444444444"
ROTATED_AVATAR = "55555555-5555-4555-8555-555555555555"


def _photo(orientation=None) -> bytes:
    """A 600×300 JPEG whose four quadrants differ, so any turn shows."""
    img = Image.new("RGB", (600, 300))
    for box, colour in (((0, 0, 300, 150), (220, 30, 30)), ((300, 0, 600, 150), (30, 200, 30)),
                        ((0, 150, 300, 300), (30, 30, 220)), ((300, 150, 600, 300), (240, 240, 40))):
        img.paste(colour, box)
    buf = io.BytesIO()
    if orientation is None:
        img.save(buf, "JPEG", quality=95)
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, "JPEG", quality=95, exif=exif)
    return buf.getvalue()


def _sideways_thumbnail(raw: bytes) -> bytes:
    """What the thumbnail code wrote before #511: the orientation tag ignored."""
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((400, 400), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _store(data_dir: Path, user_id: int, kind: str, entry_id: int, name: str,
           raw: bytes, thumb: bytes) -> Path:
    folder = data_dir / "users" / str(user_id) / kind / str(entry_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.jpg").write_bytes(raw)
    (folder / f"{name}_thumb.jpg").write_bytes(thumb)
    return folder / f"{name}_thumb.jpg"


@pytest.fixture
def engine(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as sess:
        for uid in (1, 2):
            sess.add(UserInfo(id=uid, email=f"u{uid}@example.com", display_name=f"U{uid}"))
        sess.commit()
    yield test_engine


@pytest.fixture
def data_dir(tmp_path, engine):
    """User 1: a memory photo tagged 6, an untagged one, a corrupt original and
    an avatar tagged 8. User 2: a journal photo tagged 3. Every thumbnail as
    the pre-#511 code wrote it; counted storage equal to the files."""
    rotated = _photo(6)
    upright = _photo()
    journal = _photo(3)
    avatar = _photo(8)
    _store(tmp_path, 1, "memories", 10, ROTATED_MEMORY, rotated, _sideways_thumbnail(rotated))
    _store(tmp_path, 1, "memories", 10, UPRIGHT_MEMORY, upright, _sideways_thumbnail(upright))
    _store(tmp_path, 1, "memories", 10, CORRUPT_MEMORY, b"not a photo", _sideways_thumbnail(upright))
    _store(tmp_path, 1, "people", 7, ROTATED_AVATAR, avatar, _sideways_thumbnail(avatar))
    _store(tmp_path, 2, "journal", 5, ROTATED_JOURNAL, journal, _sideways_thumbnail(journal))
    with Session(engine) as sess:
        for uid in (1, 2):
            sess.add(UserUsage(user_info_id=uid, storage_bytes=_on_disk(tmp_path, uid)))
        sess.commit()
    return tmp_path


def _on_disk(data_dir: Path, user_id: int) -> int:
    return dir_size(data_dir / "users" / str(user_id))


def _usage(engine, user_id: int) -> int:
    with Session(engine) as sess:
        return sess.exec(select(UserUsage).where(UserUsage.user_info_id == user_id)).one().storage_bytes


def _snapshot(data_dir: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(data_dir.rglob("*")) if p.is_file()}


def _thumb(data_dir: Path, user_id: int, kind: str, entry_id: int, name: str) -> Path:
    return data_dir / "users" / str(user_id) / kind / str(entry_id) / f"{name}_thumb.jpg"


def _is_upright(thumb: Path, original: Path) -> bool:
    shown = ImageOps.exif_transpose(Image.open(original)).convert("RGB")
    stored = Image.open(thumb).convert("RGB")
    if stored.size != (shown.width * 400 // max(shown.size), shown.height * 400 // max(shown.size)):
        return False
    means = ImageStat.Stat(ImageChops.difference(stored, shown.resize(stored.size))).mean
    return sum(means) / len(means) < 10


def _run(*argv) -> int:
    return backfill.main(list(argv))


def test_dry_run_reports_and_changes_nothing(data_dir, engine, capsys):
    before = _snapshot(data_dir)
    assert _run("--data-dir", str(data_dir)) == 0
    out = capsys.readouterr().out
    assert _snapshot(data_dir) == before
    assert (_usage(engine, 1), _usage(engine, 2)) == (_on_disk(data_dir, 1), _on_disk(data_dir, 2))
    assert ("scanned 4 / candidates 2 / already upright 0 / to rewrite 2 / skipped 1") in out
    assert ROTATED_MEMORY in out and ROTATED_JOURNAL in out and CORRUPT_MEMORY in out
    assert ROTATED_AVATAR not in out


def test_apply_rewrites_exactly_the_rotated_memory_and_journal_thumbnails(data_dir, engine, capsys):
    before = _snapshot(data_dir)
    assert _run("--data-dir", str(data_dir), "--apply", "--api-stopped") == 0
    out = capsys.readouterr().out
    assert "scanned 4 / candidates 2 / already upright 0 / rewritten 2 / skipped 1" in out

    after = _snapshot(data_dir)
    changed = {p for p in after if after[p] != before.get(p)}
    memory = _thumb(data_dir, 1, "memories", 10, ROTATED_MEMORY)
    journal = _thumb(data_dir, 2, "journal", 5, ROTATED_JOURNAL)
    assert changed == {memory, journal}
    assert set(after) == set(before)  # no temp file left behind, nothing added
    for thumb, name in ((memory, ROTATED_MEMORY), (journal, ROTATED_JOURNAL)):
        assert _is_upright(thumb, thumb.with_name(f"{name}.jpg"))
    # The avatar is still sideways: it is not this script's to fix.
    avatar = _thumb(data_dir, 1, "people", 7, ROTATED_AVATAR)
    assert not _is_upright(avatar, avatar.with_name(f"{ROTATED_AVATAR}.jpg"))
    # Counted storage moved by exactly the size difference, per user.
    assert (_usage(engine, 1), _usage(engine, 2)) == (_on_disk(data_dir, 1), _on_disk(data_dir, 2))


def test_second_apply_rewrites_nothing(data_dir, engine, capsys):
    assert _run("--data-dir", str(data_dir), "--apply", "--api-stopped") == 0
    capsys.readouterr()
    before = _snapshot(data_dir)
    usage = (_usage(engine, 1), _usage(engine, 2))

    assert _run("--data-dir", str(data_dir), "--apply", "--api-stopped") == 0
    out = capsys.readouterr().out
    assert "scanned 4 / candidates 2 / already upright 2 / rewritten 0 / skipped 1" in out
    assert _snapshot(data_dir) == before
    assert (_usage(engine, 1), _usage(engine, 2)) == usage


def test_a_thumbnail_written_by_todays_upload_path_is_already_upright(tmp_path, engine, capsys):
    folder = tmp_path / "users" / "1" / "memories" / "3"
    write_photo_files(folder, ROTATED_MEMORY, _photo(8))
    before = _snapshot(tmp_path)
    assert _run("--data-dir", str(tmp_path), "--apply", "--api-stopped") == 0
    assert "already upright 1 / rewritten 0" in capsys.readouterr().out
    assert _snapshot(tmp_path) == before


def test_apply_without_api_stopped_refuses_and_writes_nothing(data_dir, engine, capsys):
    before = _snapshot(data_dir)
    assert _run("--data-dir", str(data_dir), "--apply") != 0
    assert "Stop the API" in capsys.readouterr().err
    assert _snapshot(data_dir) == before
    assert (_usage(engine, 1), _usage(engine, 2)) == (_on_disk(data_dir, 1), _on_disk(data_dir, 2))


def test_a_leftover_temp_file_is_reported_and_kept(data_dir, engine, capsys):
    """A run killed between writing a temp file and renaming it leaves the file
    behind, where the nightly reconcile counts it against the owner. It is
    reported, never deleted, and the rest of the run goes on as usual."""
    leftover = data_dir / "users" / "2" / "journal" / "5" / f".{ROTATED_JOURNAL}_thumb.k3x9q_.tmp"
    leftover.write_bytes(b"half a thumbnail")
    assert _run("--data-dir", str(data_dir), "--apply", "--api-stopped") == 0
    out = capsys.readouterr().out
    assert f"leftover temp file: {leftover}" in out
    assert ("scanned 4 / candidates 2 / already upright 0 / rewritten 2 / skipped 1"
            " / leftover temp files 1") in out
    assert leftover.read_bytes() == b"half a thumbnail"
    journal = _thumb(data_dir, 2, "journal", 5, ROTATED_JOURNAL)
    assert _is_upright(journal, journal.with_name(f"{ROTATED_JOURNAL}.jpg"))


def test_the_temp_file_a_killed_rewrite_leaves_is_the_one_reported(data_dir, engine, capsys,
                                                                    monkeypatch):
    """The guard's pattern matches the name the script really gives its temp
    files: a rewrite stopped before its rename is found by the next run."""
    monkeypatch.setattr(backfill.os, "replace", lambda src, dst: None)
    assert _run("--data-dir", str(data_dir), "--apply", "--api-stopped") == 0
    monkeypatch.undo()
    capsys.readouterr()
    assert _run("--data-dir", str(data_dir)) == 0
    assert "leftover temp files 2" in capsys.readouterr().out


@pytest.mark.parametrize("script", ["scripts/backfill_thumbnail_orientation.py",
                                    "scripts/reorder_polarsteps_memory_photos.py"])
def test_the_photo_repair_scripts_are_in_the_image(script):
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert (ROOT / script).is_file()
    assert not _dockerignore_excludes(patterns, script)
