"""Tests for the shared photo replace core, ``api/photo_replace.py`` (issue #33,
U8 of docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

Covers:
  * storage is checked against the net change, so a replace that fits only
    once the old file is freed is accepted, and one that does not fit even
    then is refused with nothing written (F3);
  * a replace of a photo with no hash gives the old file's sha256 to the new
    photo, and one with a hash carries that hash (D6);
  * the source given is recorded for the new photo, and the old one's is
    never carried;
  * a photo, or a row, deleted while the new files are written leaves no files
    and no counted storage, and raises ``PhotoGone``;
  * a download of a photo a replace stood in for is skipped (issue #566);
  * the same rules hold for a journal entry;
  * stored originals are served with their own media type, a PNG as
    ``image/png``, thumbnails' fallback to the original included.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as mem_mod
import api.photo_replace as photo_replace_mod
import models.db as db_module
from api.deps import get_current_user
from api.photo_order import dump_state, load_state
from api.photo_replace import PhotoGone, replace_photo_bytes
from models.billing import UserUsage
from models.project_db import DBJournalEntry, DBMemory, DBProject
from models.user import UserInfo
from src.exceptions.errors import QuotaExceeded

_MB = 1024 * 1024
_OLD = "00000000-0000-4000-8000-0000000000a1"
_OTHER = "00000000-0000-4000-8000-0000000000a2"
_MODELS = {"memory": DBMemory, "journal": DBJournalEntry}
_FOLDERS = {"memory": "memories", "journal": "journal"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    # File-backed: the routes run the core in a worker thread.
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'replace.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        project = DBProject(user_info_id=user.id, name="My Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        ids = {"user": user.id, "project": project.id}
        for kind, model in _MODELS.items():
            row = model(project_id=project.id, date="2025-06-01", geo_mode="custom",
                        photos_json=json.dumps([_OLD, _OTHER]))
            sess.add(row)
            sess.commit()
            sess.refresh(row)
            ids[kind] = row.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(ids["user"]),
                                                          "email": "alice@example.com"}
    app.include_router(mem_mod.router)
    app.include_router(journal_mod.router)
    yield TestClient(app), engine, ids, tmp_path
    engine.dispose()


def _image_bytes(fmt: str = "JPEG", shade: int = 10) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (shade, 20, 30)).save(buf, fmt)
    return buf.getvalue()


def _folder(tmp_path, ids, kind) -> Path:
    return tmp_path / "users" / str(ids["user"]) / _FOLDERS[kind] / str(ids[kind])


def _write_old(tmp_path, ids, kind, full: bytes) -> Path:
    """Put the old photo's files on disk, as an earlier upload left them."""
    folder = _folder(tmp_path, ids, kind)
    folder.mkdir(parents=True, exist_ok=True)
    for name in (_OLD, _OTHER):
        (folder / f"{name}.jpg").write_bytes(full if name == _OLD else _image_bytes())
        (folder / f"{name}_thumb.jpg").write_bytes(_image_bytes())
    return folder / f"{_OLD}.jpg"


def _set_state(engine, ids, kind, **fields) -> None:
    state = load_state(None)
    state.update(fields)
    with Session(engine) as sess:
        row = sess.get(_MODELS[kind], ids[kind])
        row.photo_order_json = dump_state(state)
        sess.add(row)
        sess.commit()


def _row(engine, ids, kind):
    with Session(engine) as sess:
        return sess.get(_MODELS[kind], ids[kind])


def _state(engine, ids, kind) -> dict:
    return load_state(_row(engine, ids, kind).photo_order_json)


def _photos(engine, ids, kind):
    row = _row(engine, ids, kind)
    return None if row is None else json.loads(row.photos_json)


def _usage(engine, user_id) -> int:
    with Session(engine) as sess:
        row = sess.exec(select(UserUsage).where(UserUsage.user_info_id == user_id)).first()
        return row.storage_bytes if row else 0


def _seed_usage(engine, user_id, used_bytes) -> None:
    with Session(engine) as sess:
        sess.add(UserUsage(user_info_id=user_id, storage_bytes=used_bytes))
        sess.commit()


def _replace(ids, kind, raw, source=None) -> str:
    return replace_photo_bytes(kind, ids[kind], _OLD, raw, owner_dir=str(ids["user"]),
                               source=source)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ── Storage: the net change (F3) ────────────────────────────────────────────

class TestNetDeltaQuota:
    def _full_plan(self, engine, ids, monkeypatch, headroom: int) -> None:
        monkeypatch.setenv("BILLING_ENABLED", "1")
        monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
        monkeypatch.setenv("FREE_MAX_STORAGE_MB", "1")
        _seed_usage(engine, ids["user"], 1 * _MB - headroom)

    def test_a_replace_that_fits_only_by_its_net_change_is_accepted(self, env, monkeypatch):
        _, engine, ids, tmp_path = env
        raw = _image_bytes()
        # The old full-size file is larger than the new photo, and the plan
        # has room for neither on its own.
        _write_old(tmp_path, ids, "memory", b"\xff" * (len(raw) * 4))
        self._full_plan(engine, ids, monkeypatch, headroom=len(raw) // 2)

        new = _replace(ids, "memory", raw)

        assert _photos(engine, ids, "memory") == [new, _OTHER]

    def test_a_replace_over_the_limit_even_after_the_old_file_is_freed_is_refused(
            self, env, monkeypatch):
        _, engine, ids, tmp_path = env
        raw = _image_bytes()
        old_size = len(raw) // 4
        _write_old(tmp_path, ids, "memory", b"\xff" * old_size)
        # Room for less than the net change.
        self._full_plan(engine, ids, monkeypatch, headroom=len(raw) - old_size - 1)

        with pytest.raises(QuotaExceeded):
            _replace(ids, "memory", raw)

        assert _photos(engine, ids, "memory") == [_OLD, _OTHER]
        assert sorted(p.name for p in _folder(tmp_path, ids, "memory").glob("*")) == sorted(
            [f"{_OLD}.jpg", f"{_OLD}_thumb.jpg", f"{_OTHER}.jpg", f"{_OTHER}_thumb.jpg"])

    def test_the_route_answers_402_only_past_the_net_change(self, env, monkeypatch):
        client, engine, ids, tmp_path = env
        raw = _image_bytes()
        _write_old(tmp_path, ids, "journal", b"\xff" * (len(raw) * 4))
        self._full_plan(engine, ids, monkeypatch, headroom=len(raw) // 2)

        resp = client.put(f"/api/journal/{ids['journal']}/photos/{_OLD}/replace",
                          files={"file": ("n.jpg", raw, "image/jpeg")})

        assert resp.status_code == 200, resp.text


# ── Hashes and sources (D6) ─────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["memory", "journal"])
class TestHashAndSource:
    def test_a_hashless_photo_hands_its_files_sha256_to_the_new_photo(self, env, kind):
        _, engine, ids, tmp_path = env
        old_bytes = _image_bytes(shade=200)
        old_full = _write_old(tmp_path, ids, kind, old_bytes)

        new = _replace(ids, kind, _image_bytes(shade=50))

        assert _state(engine, ids, kind)["hashes"] == {new: _sha(old_bytes)}
        assert not old_full.exists()

    def test_a_recorded_hash_is_carried_not_recomputed(self, env, kind):
        _, engine, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes(shade=200))
        recorded = "ab" * 32
        _set_state(engine, ids, kind, hashes={_OLD: recorded})

        new = _replace(ids, kind, _image_bytes(shade=50))

        assert _state(engine, ids, kind)["hashes"] == {new: recorded}

    def test_the_source_is_recorded_and_the_old_one_is_not_carried(self, env, kind):
        _, engine, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes(shade=200))
        _set_state(engine, ids, kind, sources={_OLD: "immich:old-asset"})

        new = _replace(ids, kind, _image_bytes(shade=50), source="immich:new-asset")

        assert _state(engine, ids, kind)["sources"] == {new: "immich:new-asset"}

    def test_no_source_records_none(self, env, kind):
        _, engine, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes(shade=200))
        _set_state(engine, ids, kind, sources={_OLD: "immich:old-asset"})

        _replace(ids, kind, _image_bytes(shade=50))

        assert _state(engine, ids, kind)["sources"] == {}

    def test_rank_and_position_are_kept(self, env, kind):
        _, engine, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes(shade=200))
        _set_state(engine, ids, kind, ranks={_OLD: 0, _OTHER: 1})

        new = _replace(ids, kind, _image_bytes(shade=50))

        assert _photos(engine, ids, kind) == [new, _OTHER]
        assert _state(engine, ids, kind)["ranks"] == {new: 0, _OTHER: 1}


def test_a_download_of_the_photo_a_replace_stood_in_for_is_skipped(env, monkeypatch):
    """The thumbnail Polarsteps served is replaced by an original; a repeated
    import fetches the thumbnail again and finds its hash on the original."""
    client, engine, ids, tmp_path = env
    thumbnail = _image_bytes(shade=200)
    _write_old(tmp_path, ids, "memory", thumbnail)
    new = _replace(ids, "memory", _image_bytes(shade=50))
    saves = []
    monkeypatch.setattr(mem_mod, "fetch_bytes", lambda url, **kwargs: thumbnail)
    monkeypatch.setattr(mem_mod, "_save_photo_files", lambda *a, **k: saves.append(a))

    resp = client.post(f"/api/memories/{ids['memory']}/photos/from-url",
                       json={"url": "http://x/0.jpg", "order": 0})

    assert resp.status_code == 202
    assert saves == []
    assert _photos(engine, ids, "memory") == [new, _OTHER]


# ── Gone meanwhile ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["memory", "journal"])
class TestGoneMidReplace:
    def _baseline(self, engine, ids) -> int:
        _seed_usage(engine, ids["user"], 1_000_000)
        return 1_000_000

    def test_a_photo_deleted_while_the_files_are_written(self, env, monkeypatch, kind):
        _, engine, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes(shade=200))
        baseline = self._baseline(engine, ids)
        original_save = photo_replace_mod.save_photo_files
        written = []

        def delete_old_then_save(user_id, folder, name, raw):
            with Session(engine) as sess:
                row = sess.get(_MODELS[kind], ids[kind])
                row.photos_json = json.dumps([_OTHER])
                sess.add(row)
                sess.commit()
            written.append(name)
            original_save(user_id, folder, name, raw)

        monkeypatch.setattr(photo_replace_mod, "save_photo_files", delete_old_then_save)
        with pytest.raises(PhotoGone):
            _replace(ids, kind, _image_bytes(shade=50))

        (new,) = written
        assert _photos(engine, ids, kind) == [_OTHER]
        assert not list(_folder(tmp_path, ids, kind).glob(f"{new}*"))
        assert _usage(engine, ids["user"]) == baseline

    def test_a_row_deleted_while_the_files_are_written(self, env, monkeypatch, kind):
        _, engine, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes(shade=200))
        baseline = self._baseline(engine, ids)
        original_save = photo_replace_mod.save_photo_files
        written = []

        def delete_row_then_save(user_id, folder, name, raw):
            with Session(engine) as sess:
                sess.delete(sess.get(_MODELS[kind], ids[kind]))
                sess.commit()
            written.append(name)
            original_save(user_id, folder, name, raw)

        monkeypatch.setattr(photo_replace_mod, "save_photo_files", delete_row_then_save)
        with pytest.raises(PhotoGone):
            _replace(ids, kind, _image_bytes(shade=50))

        (new,) = written
        assert not list(_folder(tmp_path, ids, kind).glob(f"{new}*"))
        assert _usage(engine, ids["user"]) == baseline

    def test_a_photo_not_listed_writes_nothing(self, env, kind):
        _, engine, ids, tmp_path = env
        folder = _folder(tmp_path, ids, kind)
        with pytest.raises(PhotoGone):
            replace_photo_bytes(kind, ids[kind], "00000000-0000-4000-8000-0000000000ff",
                                _image_bytes(), owner_dir=str(ids["user"]))
        assert not folder.exists()


# ── Served media types (F4) ─────────────────────────────────────────────────

@pytest.mark.parametrize("kind,prefix", [("memory", "memories"), ("journal", "journal")])
class TestServedMediaType:
    def test_a_png_original_is_served_as_png(self, env, kind, prefix):
        client, _, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes("PNG"))

        resp = client.get(f"/api/{prefix}/{ids[kind]}/photos/{_OLD}")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"

    def test_a_jpeg_original_is_still_served_as_jpeg(self, env, kind, prefix):
        client, _, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes())

        resp = client.get(f"/api/{prefix}/{ids[kind]}/photos/{_OLD}")

        assert resp.headers["content-type"] == "image/jpeg"

    def test_a_missing_thumbnail_falls_back_to_the_original_with_its_type(
            self, env, kind, prefix):
        client, _, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes("PNG"))
        (_folder(tmp_path, ids, kind) / f"{_OLD}_thumb.jpg").unlink()

        resp = client.get(f"/api/{prefix}/{ids[kind]}/photos/{_OLD}/thumb")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"

    def test_a_thumbnail_is_served_as_jpeg(self, env, kind, prefix):
        client, _, ids, tmp_path = env
        _write_old(tmp_path, ids, kind, _image_bytes("PNG"))

        resp = client.get(f"/api/{prefix}/{ids[kind]}/photos/{_OLD}/thumb")

        assert resp.headers["content-type"] == "image/jpeg"
