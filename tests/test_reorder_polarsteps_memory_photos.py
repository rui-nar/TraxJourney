"""Tests for scripts/reorder_polarsteps_memory_photos.py (issue #237 backfill).

Covers the two layers of the script:
  * ``plan_memory_reorder`` — the pure content-hash-matching logic: correct
    reordering when hashes match, unmatched (e.g. manually added) photos
    preserved and appended rather than dropped, and memories flagged for
    manual review rather than guessed at when matching is unreliable;
  * ``main()`` end-to-end against a seeded SQLite DB — dry-run makes no DB
    changes, ``--apply`` persists the corrected ``photos_json``, and a
    project with no linked trip and no override is skipped untouched;
  * the rank model's ``photo_order_json`` — ``--apply`` writes a dense list,
    clears ranks and keeps the epoch, an unmigrated DB is refused, and a
    memory edited by the live API during the run is not overwritten.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import requests
from sqlmodel import Session, SQLModel, create_engine

from models.project_db import DBMemory, DBProject, DBProjectSyncMeta
from models.user import PolarstepsToken, UserInfo

import scripts.reorder_polarsteps_memory_photos as backfill


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Pure logic: plan_memory_reorder ─────────────────────────────────────────

class TestPlanMemoryReorder:
    def test_reorders_by_content_hash_regardless_of_stored_order(self):
        # Stored (scrambled) order: c, a, b. Source (correct) order: a, b, c.
        content = {"a": b"AAA", "b": b"BBB", "c": b"CCC"}
        current = ["c", "a", "b"]
        source_photos = [{"url": f"http://x/{k}"} for k in ("a", "b", "c")]
        local_hashes = {u: _sha256(content[u]) for u in current}
        download = lambda url: content[url.rsplit("/", 1)[-1]]

        new_order, note = backfill.plan_memory_reorder(current, source_photos, local_hashes, download)
        assert new_order == ["a", "b", "c"]
        assert "->" in note

    def test_unmatched_local_photo_is_preserved_and_appended(self):
        # "extra" was added manually after import — it has no source-side match.
        content = {"a": b"AAA", "b": b"BBB"}
        current = ["b", "extra", "a"]
        source_photos = [{"url": "http://x/a"}, {"url": "http://x/b"}]
        local_hashes = {
            "a": _sha256(content["a"]),
            "b": _sha256(content["b"]),
            "extra": _sha256(b"manual-photo-bytes"),
        }
        download = lambda url: content[url.rsplit("/", 1)[-1]]

        new_order, note = backfill.plan_memory_reorder(current, source_photos, local_hashes, download)
        assert new_order == ["a", "b", "extra"]

    def test_already_correct_order_is_a_noop(self):
        content = {"a": b"AAA", "b": b"BBB"}
        current = ["a", "b"]
        source_photos = [{"url": "http://x/a"}, {"url": "http://x/b"}]
        local_hashes = {u: _sha256(content[u]) for u in current}
        download = lambda url: content[url.rsplit("/", 1)[-1]]

        new_order, note = backfill.plan_memory_reorder(current, source_photos, local_hashes, download)
        assert new_order is None
        assert "already" in note

    def test_majority_download_failure_flags_for_manual_review(self):
        current = ["a", "b"]
        source_photos = [{"url": "http://x/a"}, {"url": "http://x/b"}, {"url": "http://x/c"}]
        local_hashes = {"a": _sha256(b"AAA"), "b": _sha256(b"BBB")}

        def flaky_download(url):
            raise ConnectionError("boom")

        new_order, note = backfill.plan_memory_reorder(current, source_photos, local_hashes, flaky_download)
        assert new_order is None
        assert "flagged for manual review" in note

    def test_mostly_unmatched_local_photos_flags_for_manual_review(self):
        # Only one local photo matches a source photo by content — the rest
        # look unrelated (e.g. the trip changed on Polarsteps since import).
        content = {"a": b"AAA"}
        current = ["a", "x", "y", "z"]
        source_photos = [{"url": "http://x/a"}]
        local_hashes = {
            "a": _sha256(content["a"]),
            "x": _sha256(b"unrelated-x"),
            "y": _sha256(b"unrelated-y"),
            "z": _sha256(b"unrelated-z"),
        }
        download = lambda url: content[url.rsplit("/", 1)[-1]]

        new_order, note = backfill.plan_memory_reorder(current, source_photos, local_hashes, download)
        assert new_order is None
        assert "flagged for manual review" in note

    def test_no_stored_photos_is_a_noop(self):
        new_order, note = backfill.plan_memory_reorder([], [{"url": "http://x/a"}], {}, lambda u: b"")
        assert new_order is None
        assert "nothing to reorder" in note


# ── End-to-end against a seeded SQLite DB ───────────────────────────────────

class _FakeClient:
    """Stands in for PolarstepsClient: returns canned raw steps for one trip."""

    def __init__(self, remember_token: str):
        # The script reads the table directly, so it must decrypt the token
        # the model stored encrypted.
        assert remember_token == "123|deadbeef"
        self.remember_token = remember_token

    def get_trip_steps(self, trip_id: int):
        assert trip_id == 555
        return [
            {
                "id": 42,
                "media": [
                    {"type": 0, "large_thumbnail_path": "http://x/a"},
                    {"type": 0, "large_thumbnail_path": "http://x/b"},
                    {"type": 0, "large_thumbnail_path": "http://x/c"},
                ],
            }
        ]


def _seed_db(path: Path, data_dir: Path, *, link_trip: bool, scrambled_photos, content,
             photo_order_json=None) -> tuple[int, int]:
    """Seed a project with one Polarsteps-imported memory. Returns (project_id, memory_id)."""
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)

        sess.add(PolarstepsToken(user_info_id=user.id, remember_token="123|deadbeef", polarsteps_user_id=123))

        project = DBProject(user_info_id=user.id, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)

        if link_trip:
            sess.add(DBProjectSyncMeta(project_id=project.id, linked_ps_trip_id=555))

        memory = DBMemory(
            project_id=project.id, date="2025-06-01", geo_mode="custom",
            polarsteps_step_id=42, photos_json=json.dumps(scrambled_photos),
            photo_order_json=photo_order_json,
        )
        sess.add(memory)
        sess.commit()
        sess.refresh(memory)

        photo_dir = data_dir / "users" / str(user.id) / "memories" / str(memory.id)
        photo_dir.mkdir(parents=True)
        for uuid_str, data in content.items():
            (photo_dir / f"{uuid_str}.jpg").write_bytes(data)

        project_id, memory_id = project.id, memory.id
    engine.dispose()
    return project_id, memory_id


_CONTENT = {"00000000-0000-4000-8000-000000000041": b"AAA-bytes", "00000000-0000-4000-8000-000000000042": b"BBB-bytes", "00000000-0000-4000-8000-000000000043": b"CCC-bytes"}
_URL_CONTENT = {"http://x/a": _CONTENT["00000000-0000-4000-8000-000000000041"], "http://x/b": _CONTENT["00000000-0000-4000-8000-000000000042"], "http://x/c": _CONTENT["00000000-0000-4000-8000-000000000043"]}


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content


def _patch_requests_get(monkeypatch) -> None:
    """main() drives run()'s default downloader, which calls requests.get — stub
    it so these end-to-end tests never touch the network."""
    monkeypatch.setattr(requests, "get", lambda url, timeout=30: _FakeResponse(_URL_CONTENT[url]))


class TestBackfillScriptEndToEnd:
    def test_dry_run_reports_but_does_not_change_the_db(self, tmp_path, monkeypatch):
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        # Stored order is scrambled: c, a, b. Correct order is a, b, c.
        _, memory_id = _seed_db(db, data_dir, link_trip=True,
                                 scrambled_photos=["00000000-0000-4000-8000-000000000043", "00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042"], content=_CONTENT)

        monkeypatch.setattr(backfill, "PolarstepsClient", _FakeClient)
        _patch_requests_get(monkeypatch)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db), "--data-dir", str(data_dir)])
        assert backfill.main() == 0

        import sqlite3
        con = sqlite3.connect(str(db))
        photos = json.loads(con.execute("SELECT photos_json FROM memory WHERE id=?", (memory_id,)).fetchone()[0])
        con.close()
        assert photos == ["00000000-0000-4000-8000-000000000043", "00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042"]  # untouched

    def test_apply_writes_the_corrected_order(self, tmp_path, monkeypatch):
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        _, memory_id = _seed_db(db, data_dir, link_trip=True,
                                 scrambled_photos=["00000000-0000-4000-8000-000000000043", "00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042"], content=_CONTENT)

        monkeypatch.setattr(backfill, "PolarstepsClient", _FakeClient)
        _patch_requests_get(monkeypatch)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db), "--data-dir", str(data_dir), "--apply"])
        assert backfill.main() == 0

        import sqlite3
        con = sqlite3.connect(str(db))
        photos = json.loads(con.execute("SELECT photos_json FROM memory WHERE id=?", (memory_id,)).fetchone()[0])
        con.close()
        assert photos == ["00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042", "00000000-0000-4000-8000-000000000043"]

    def test_project_without_linked_trip_or_override_is_skipped(self, tmp_path, monkeypatch):
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        _, memory_id = _seed_db(db, data_dir, link_trip=False,
                                 scrambled_photos=["00000000-0000-4000-8000-000000000043", "00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042"], content=_CONTENT)

        monkeypatch.setattr(backfill, "PolarstepsClient", _FakeClient)
        _patch_requests_get(monkeypatch)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db), "--data-dir", str(data_dir), "--apply"])
        assert backfill.main() == 0

        import sqlite3
        con = sqlite3.connect(str(db))
        photos = json.loads(con.execute("SELECT photos_json FROM memory WHERE id=?", (memory_id,)).fetchone()[0])
        con.close()
        assert photos == ["00000000-0000-4000-8000-000000000043", "00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042"]  # untouched, no trip to check against

    def test_project_trip_override_resolves_an_unlinked_project(self, tmp_path, monkeypatch):
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        project_id, memory_id = _seed_db(db, data_dir, link_trip=False,
                                          scrambled_photos=["00000000-0000-4000-8000-000000000043", "00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042"], content=_CONTENT)

        monkeypatch.setattr(backfill, "PolarstepsClient", _FakeClient)
        _patch_requests_get(monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["x", "--db", str(db), "--data-dir", str(data_dir), "--apply",
             "--project-trip", f"{project_id}:555"],
        )
        assert backfill.main() == 0

        import sqlite3
        con = sqlite3.connect(str(db))
        photos = json.loads(con.execute("SELECT photos_json FROM memory WHERE id=?", (memory_id,)).fetchone()[0])
        con.close()
        assert photos == ["00000000-0000-4000-8000-000000000041", "00000000-0000-4000-8000-000000000042", "00000000-0000-4000-8000-000000000043"]


# ── photo_order_json (issue #237 rank model) ────────────────────────────────

_A, _B, _C = (f"00000000-0000-4000-8000-00000000004{i}" for i in (1, 2, 3))


def _read_memory(db: Path, memory_id: int) -> tuple[list, dict]:
    import sqlite3
    con = sqlite3.connect(str(db))
    photos_json, order_json = con.execute(
        "SELECT photos_json, photo_order_json FROM memory WHERE id=?", (memory_id,)
    ).fetchone()
    con.close()
    return json.loads(photos_json), (json.loads(order_json) if order_json else None)


class TestPhotoOrderColumn:
    def test_apply_resets_stale_ranks_keeps_epoch_and_writes_no_null(self, tmp_path, monkeypatch):
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        # A pre-migration copy can still hold a null slot; the ranks are stale
        # (they describe the scrambled order) and a re-import bumped the epoch.
        stale = json.dumps({"epoch": 3, "ranks": {_C: 0, _A: 1, _B: 2}})
        _, memory_id = _seed_db(db, data_dir, link_trip=True,
                                scrambled_photos=[_C, None, _A, _B], content=_CONTENT,
                                photo_order_json=stale)

        monkeypatch.setattr(backfill, "PolarstepsClient", _FakeClient)
        _patch_requests_get(monkeypatch)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db), "--data-dir", str(data_dir), "--apply"])
        assert backfill.main() == 0

        photos, state = _read_memory(db, memory_id)
        assert photos == [_A, _B, _C]
        assert None not in photos
        assert state == {"epoch": 3, "ranks": {}}

    def test_apply_on_null_photo_order_json_writes_epoch_zero(self, tmp_path, monkeypatch):
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        _, memory_id = _seed_db(db, data_dir, link_trip=True,
                                scrambled_photos=[_C, _A, _B], content=_CONTENT)

        monkeypatch.setattr(backfill, "PolarstepsClient", _FakeClient)
        _patch_requests_get(monkeypatch)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db), "--data-dir", str(data_dir), "--apply"])
        assert backfill.main() == 0

        photos, state = _read_memory(db, memory_id)
        assert photos == [_A, _B, _C]
        assert state == {"epoch": 0, "ranks": {}}

    def test_unmigrated_db_is_refused_before_anything_runs(self, tmp_path, monkeypatch, capsys):
        import sqlite3
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        _, memory_id = _seed_db(db, data_dir, link_trip=True,
                                scrambled_photos=[_C, _A, _B], content=_CONTENT)
        con = sqlite3.connect(str(db))
        con.execute("ALTER TABLE memory DROP COLUMN photo_order_json")
        con.commit()
        con.close()

        def _no_client(_token):
            raise AssertionError("an unmigrated DB must be refused before any Polarsteps call")

        monkeypatch.setattr(backfill, "PolarstepsClient", _no_client)
        monkeypatch.setattr("sys.argv", ["x", "--db", str(db), "--data-dir", str(data_dir), "--apply"])
        assert backfill.main() == 2
        assert "photo_order_json" in capsys.readouterr().err

        con = sqlite3.connect(str(db))
        photos = json.loads(con.execute("SELECT photos_json FROM memory WHERE id=?", (memory_id,)).fetchone()[0])
        con.close()
        assert photos == [_C, _A, _B]

    def test_memory_edited_during_the_run_is_left_untouched(self, tmp_path):
        # The script runs with the API live: an upload landing between the
        # read and the write must survive, not be overwritten by the plan.
        import sqlite3
        db = tmp_path / "r.db"
        data_dir = tmp_path / "data"
        _, memory_id = _seed_db(db, data_dir, link_trip=True,
                                scrambled_photos=[_C, _A, _B], content=_CONTENT)
        live = [_C, _A, _B, "00000000-0000-4000-8000-000000000099"]
        edited = []

        def download_while_the_api_writes(url):
            if not edited:
                api = sqlite3.connect(str(db))
                api.execute("UPDATE memory SET photos_json=? WHERE id=?", (json.dumps(live), memory_id))
                api.commit()
                api.close()
                edited.append(True)
            return _URL_CONTENT[url]

        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        changed = backfill.run(con, data_dir, True, {}, client_factory=_FakeClient,
                               download=download_while_the_api_writes)
        con.close()

        assert changed == 0
        photos, state = _read_memory(db, memory_id)
        assert photos == live
        assert state is None
