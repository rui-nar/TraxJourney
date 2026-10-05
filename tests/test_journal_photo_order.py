"""Journal photo placement by rank (issue #237, plan U5).

The journal mirrors ``api/memories.py``: a photo's ``order`` is a *rank*
(``api/photo_order.py``), inserted among the ranked photos and never written
over another one; deletes do not shift later downloads; replace keeps the old
photo's position and rank; ``order`` is bounded to 0..9999. A photo that
cannot be placed (entry gone, or for replace the old photo gone) leaves no
files and no counted storage behind, and upload/replace answer 404.

Journal photos live under the entry's *author*'s tree, not the project
owner's. Every test here acts as a travel companion who authors the entry on
the owner's trip, so a cleanup that looked in the owner's tree, or uncounted
the owner, would be caught: the owner's counted storage must never move.
"""
from __future__ import annotations

import io
import json
import random
import threading
import uuid as uuid_lib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.photo_order import load_state
from models.billing import UserUsage
from models.project_db import DBJournalEntry, DBProject, DBProjectMember
from models.user import UserInfo

# Counted storage before any photo lands; non-zero so a cleanup that
# subtracts too much is caught (the counter clamps at zero).
_BASELINE_USAGE = 1_000_000


@pytest.fixture
def env(monkeypatch, tmp_path):
    # File-backed, one connection per session: several tests run requests and
    # writers on separate threads, which an in-memory StaticPool engine (one
    # shared connection) cannot do reliably.
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'journal_order.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@example.com")
        author = UserInfo(display_name="Companion", email="comp@example.com")
        sess.add(owner)
        sess.add(author)
        sess.commit()
        sess.refresh(owner)
        sess.refresh(author)

        project = DBProject(user_info_id=owner.id, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        sess.add(DBProjectMember(project_id=project.id, user_info_id=author.id,
                                 role="editor", invited_by=owner.id, created_at=0.0))
        sess.add(UserUsage(user_info_id=owner.id, storage_bytes=_BASELINE_USAGE))
        sess.add(UserUsage(user_info_id=author.id, storage_bytes=_BASELINE_USAGE))
        sess.commit()
        ids = {"owner": owner.id, "author": author.id}

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: _user(ids["author"])
    app.include_router(journal_router)
    client = TestClient(app)
    ids["journal"] = _new_entry(ids, "2025-06-01")
    yield client, engine, ids
    engine.dispose()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _user(user_id) -> dict:
    return {"sub": str(user_id), "email": "comp@example.com"}


def _new_entry(ids, date: str) -> int:
    """The companion creates an entry on the owner's trip (its author is them)."""
    body = journal_mod.JournalBody(project_name="Trip", date=date, geo_mode="custom")
    return journal_mod.create_journal(body, _user(ids["author"]), owner=ids["owner"])["id"]


def _delete_entry(ids, journal_id: int) -> None:
    journal_mod.delete_journal(journal_id, _user(ids["author"]))


def _photos(engine, journal_id):
    with Session(engine) as sess:
        row = sess.get(DBJournalEntry, journal_id)
    return None if row is None else json.loads(row.photos_json)


def _ranks(engine, journal_id) -> dict:
    with Session(engine) as sess:
        return load_state(sess.get(DBJournalEntry, journal_id).photo_order_json)["ranks"]


def _usage(engine, user_id) -> int:
    with Session(engine) as sess:
        return sess.exec(select(UserUsage).where(UserUsage.user_info_id == user_id)).one().storage_bytes


def _files(user_id, journal_id) -> list:
    folder = Path(journal_mod._DATA_DIR) / "users" / str(user_id) / "journal" / str(journal_id)
    return sorted(p.name for p in folder.glob("*")) if folder.exists() else []


def _assert_nothing_left(engine, ids, *journal_ids) -> None:
    """No file in either user's tree, and neither user's counted storage moved."""
    for journal_id in journal_ids:
        assert _files(ids["author"], journal_id) == []
        assert _files(ids["owner"], journal_id) == []
    assert _usage(engine, ids["author"]) == _BASELINE_USAGE
    assert _usage(engine, ids["owner"]) == _BASELINE_USAGE


def _land(ids, journal_id, order=None, epoch=None, uuid_str=None) -> str:
    """Write a photo's files and place it — what a finished download does."""
    author = str(ids["author"])
    uuid_str = uuid_str or str(uuid_lib.uuid4())
    journal_mod._save_photo_files(author, journal_id, uuid_str, _jpeg_bytes())
    journal_mod._write_journal_photo(journal_id, uuid_str, order, epoch, owner_dir=author)
    return uuid_str


# ── Placement by rank ───────────────────────────────────────────────────────

class TestRankPlacement:
    def test_a_manual_upload_during_import_is_not_overwritten(self, env):
        client, engine, ids = env
        journal_id = ids["journal"]
        first = _land(ids, journal_id, order=0)
        resp = client.post(f"/api/journal/{journal_id}/photos",
                           files={"file": ("m.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 201, resp.text
        manual = resp.json()["uuid"]
        # The download for order 1 lands where the manual photo sits (index 1):
        # it used to overwrite it.
        second = _land(ids, journal_id, order=1)

        assert _photos(engine, journal_id) == [first, second, manual]
        assert len(_files(ids["author"], journal_id)) == 6

    def test_a_delete_during_import_does_not_shift_later_downloads(self, env):
        client, engine, ids = env
        journal_id = ids["journal"]
        p0 = _land(ids, journal_id, order=0)
        p1 = _land(ids, journal_id, order=1)
        p3 = _land(ids, journal_id, order=3)

        assert client.delete(f"/api/journal/{journal_id}/photos/{p1}").status_code == 204
        # Order 2 lands after the delete: index 2 is now p3's slot, which the
        # old positional write overwrote.
        p2 = _land(ids, journal_id, order=2)

        assert _photos(engine, journal_id) == [p0, p2, p3]
        assert p1 not in _ranks(engine, journal_id)

    def test_concurrent_downloads_lose_nothing(self, env):
        _, engine, ids = env
        journal_id = ids["journal"]
        n = 20
        ranks = list(range(n))
        random.Random(237).shuffle(ranks)
        uuids = {rank: str(uuid_lib.uuid4()) for rank in ranks}
        threads = [
            threading.Thread(target=_land, args=(ids, journal_id, rank, None, uuids[rank]))
            for rank in ranks
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert _photos(engine, journal_id) == [uuids[r] for r in range(n)]

    def test_replace_keeps_position_and_rank(self, env):
        client, engine, ids = env
        journal_id = ids["journal"]
        p0 = _land(ids, journal_id, order=0)
        p1 = _land(ids, journal_id, order=1)
        p2 = _land(ids, journal_id, order=2)

        resp = client.put(f"/api/journal/{journal_id}/photos/{p1}/replace",
                          files={"file": ("n.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 200, resp.text
        new = resp.json()["uuid"]

        assert _photos(engine, journal_id) == [p0, new, p2]
        ranks = _ranks(engine, journal_id)
        assert ranks[new] == 1 and p1 not in ranks
        # A late download for rank 1 goes after the replacement, not over it.
        late = _land(ids, journal_id, order=1)
        assert _photos(engine, journal_id) == [p0, new, late, p2]


# ── Photos for an entry (or photo) deleted meanwhile ────────────────────────

class TestDeletedEntry:
    def test_a_download_for_a_deleted_entry_leaves_no_files(self, env, monkeypatch):
        client, engine, ids = env
        journal_id = ids["journal"]

        def fetch_then_delete(url, **kwargs):
            _delete_entry(ids, journal_id)
            return _jpeg_bytes()

        monkeypatch.setattr(journal_mod, "fetch_bytes", fetch_then_delete)
        resp = client.post(f"/api/journal/{journal_id}/photos/from-url",
                           json={"url": "http://x/0.jpg", "order": 0})
        assert resp.status_code == 202

        assert _photos(engine, journal_id) is None
        _assert_nothing_left(engine, ids, journal_id)

    def test_a_download_for_a_deleted_entry_does_not_land_in_a_new_entry(self, env, monkeypatch):
        client, engine, ids = env
        journal_id = ids["journal"]
        created = []

        def fetch_then_recreate(url, **kwargs):
            _delete_entry(ids, journal_id)
            created.append(_new_entry(ids, "2025-06-02"))
            return _jpeg_bytes()

        monkeypatch.setattr(journal_mod, "fetch_bytes", fetch_then_recreate)
        resp = client.post(f"/api/journal/{journal_id}/photos/from-url", json={"url": "http://x/0.jpg"})
        assert resp.status_code == 202

        (new_id,) = created
        assert new_id != journal_id
        assert _photos(engine, new_id) == []
        _assert_nothing_left(engine, ids, journal_id, new_id)

    def test_an_upload_for_a_deleted_entry_does_not_land_in_a_new_entry(self, env, monkeypatch):
        client, engine, ids = env
        journal_id = ids["journal"]
        original_save = journal_mod._save_photo_files
        created = []

        def recreate_then_save(*args):
            # The newest entry is deleted during the upload's file write and a
            # new one is created: without AUTOINCREMENT it would reuse the id.
            _delete_entry(ids, journal_id)
            created.append(_new_entry(ids, "2025-06-02"))
            original_save(*args)

        monkeypatch.setattr(journal_mod, "_save_photo_files", recreate_then_save)
        resp = client.post(f"/api/journal/{journal_id}/photos",
                           files={"file": ("m.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 404

        (new_id,) = created
        assert new_id != journal_id
        assert _photos(engine, new_id) == []
        _assert_nothing_left(engine, ids, journal_id, new_id)

    def test_replacing_a_photo_deleted_meanwhile_answers_404_and_leaves_no_files(self, env, monkeypatch):
        client, engine, ids = env
        journal_id = ids["journal"]
        old = _land(ids, journal_id, order=0)
        original_save = journal_mod._save_photo_files

        def delete_old_then_save(*args):
            journal_mod.delete_photo(journal_id, old, _user(ids["author"]))
            original_save(*args)

        monkeypatch.setattr(journal_mod, "_save_photo_files", delete_old_then_save)
        resp = client.put(f"/api/journal/{journal_id}/photos/{old}/replace",
                          files={"file": ("n.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 404

        assert _photos(engine, journal_id) == []
        _assert_nothing_left(engine, ids, journal_id)

    def test_replacing_a_photo_of_an_entry_deleted_meanwhile_answers_404_and_leaves_no_files(
            self, env, monkeypatch):
        client, engine, ids = env
        journal_id = ids["journal"]
        old = _land(ids, journal_id, order=0)
        original_save = journal_mod._save_photo_files

        def delete_entry_then_save(*args):
            _delete_entry(ids, journal_id)
            original_save(*args)

        monkeypatch.setattr(journal_mod, "_save_photo_files", delete_entry_then_save)
        resp = client.put(f"/api/journal/{journal_id}/photos/{old}/replace",
                          files={"file": ("n.jpg", _jpeg_bytes(), "image/jpeg")})
        assert resp.status_code == 404

        assert _photos(engine, journal_id) is None
        _assert_nothing_left(engine, ids, journal_id)

    def test_a_placement_during_the_delete_lands_before_it_or_not_at_all(self, env, monkeypatch):
        _, engine, ids = env
        journal_id = ids["journal"]
        author = str(ids["author"])
        _land(ids, journal_id, order=0)
        # A photo whose files are written (and counted) before the delete starts.
        racer = str(uuid_lib.uuid4())
        journal_mod._save_photo_files(author, journal_id, racer, _jpeg_bytes())

        original_bump = journal_mod.bump_lock_version
        racers = []

        def place_mid_delete(sess, project_id):
            # The delete has read its photo list and unlinked it, and has not
            # committed: the racer tries to place now. Without the delete
            # holding the photo lock it would land in the row about to go,
            # and its files would never be deleted.
            t = threading.Thread(target=journal_mod._write_journal_photo,
                                 args=(journal_id, racer), kwargs={"owner_dir": author})
            t.start()
            racers.append(t)
            t.join(timeout=0.5)
            original_bump(sess, project_id)

        monkeypatch.setattr(journal_mod, "bump_lock_version", place_mid_delete)
        _delete_entry(ids, journal_id)
        for t in racers:
            t.join()

        assert _photos(engine, journal_id) is None
        _assert_nothing_left(engine, ids, journal_id)


# ── The from-url route ──────────────────────────────────────────────────────

class TestFromUrlRoute:
    def test_order_out_of_range_is_refused(self, env, monkeypatch):
        client, engine, ids = env
        journal_id = ids["journal"]
        monkeypatch.setattr(journal_mod, "fetch_bytes", lambda url, **kwargs: _jpeg_bytes())

        for order in (-1, 10000):
            resp = client.post(f"/api/journal/{journal_id}/photos/from-url",
                               json={"url": "http://x/bad.jpg", "order": order})
            assert resp.status_code == 422, order
        assert _photos(engine, journal_id) == []

        for order in (9999, 0):
            resp = client.post(f"/api/journal/{journal_id}/photos/from-url",
                               json={"url": f"http://x/{order}.jpg", "order": order})
            assert resp.status_code == 202, order
        ranks = _ranks(engine, journal_id)
        assert [ranks[p] for p in _photos(engine, journal_id)] == [0, 9999]
        # The downloads landed in the author's tree, not the owner's.
        assert len(_files(ids["author"], journal_id)) == 4
        assert _files(ids["owner"], journal_id) == []

    def test_a_download_queued_under_another_epoch_is_dropped(self, env):
        # Nothing bumps a journal entry's epoch today; the writer still keeps
        # the memories rule, so a mismatching epoch drops the photo cleanly.
        _, engine, ids = env
        journal_id = ids["journal"]
        _land(ids, journal_id, order=0, epoch=1)

        assert _photos(engine, journal_id) == []
        _assert_nothing_left(engine, ids, journal_id)
