"""The scan job chain of the Immich photo upgrade (issue #33, U9 of
docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

Immich is a fake client; trip photos are real files in a temporary data
directory; the database is a file, since thumbnails are fetched from threads.
Covers:
  * a 600-photo album taking three fingerprint jobs;
  * suggestions written with ``selected`` per confidence and ``position`` in
    trip order;
  * a companion's scan covering only their own journal entries;
  * rejected pairs carried over from an earlier scan;
  * earlier scans deleted only once the new one is ``ready``, and kept while
    their apply is ``pending`` or ``running``;
  * a failed scan leaving the earlier scan and its rejections in place;
  * ``ImmichTooManyAssets`` and the 5,000-photo limit failing the scan;
  * Immich unreachable, and a 403;
  * a failed thumbnail fetch counted;
  * a fetch that can pass tried again in later passes, a permanent one not
    (INT-R1-A1);
  * Immich down judged over the whole scan: nothing fingerprinted and a
    failure that can pass fails the scan and keeps the earlier review
    (INT-R2-1, INT-R2-2);
  * a scan left with ``_UNREAD_FAIL_PERCENT`` of its album unread after a
    retry pass failing and keeping the earlier review, one under it going
    ready (INT-R3-1, INT-R3-2);
  * a project deleted mid-chain stopping the next job quietly;
  * the killed-work-horse handler and the worker's startup sweep;
  * the in-process loop without a broker.
"""
from __future__ import annotations

import io
import json
import random
import time
import uuid as uuid_lib
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as mem_mod
import models.db as db_module
import src.jobs.worker as worker_mod
import src.photo_upgrade.scan_job as scan_job
from api.photo_order import dump_state, load_state
from models.project_db import (
    DBJournalEntry,
    DBMemory,
    DBPhotoUpgradeAsset,
    DBPhotoUpgradeScan,
    DBPhotoUpgradeSuggestion,
    DBProject,
    DBProjectMember,
    delete_photo_upgrade_scans,
)
from models.user import ImmichToken, UserInfo
from src.immich.client import (
    Asset,
    ImmichAddressNotAllowed,
    ImmichBadAnswer,
    ImmichRejected,
    ImmichTooManyAssets,
    ImmichUnreachable,
)
from src.project.project_repo import ProjectRepo

_DAY = "2026-05-02"


# ── Images ────────────────────────────────────────────────────────────────────

def _pattern(seed: int, size=(400, 300)) -> Image.Image:
    """A blocky image whose hashes are far from any other seed's."""
    rnd = random.Random(seed)
    small = Image.new("RGB", (8, 6))
    small.putdata([tuple(rnd.randrange(256) for _ in range(3)) for _ in range(48)])
    return small.resize(size, Image.NEAREST)


def _encode(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _album_thumb(seed: int, size=(332, 249)) -> bytes:
    """What Immich's ``size=thumbnail`` answers for the photo: a WebP (OD1)."""
    return _encode(_pattern(seed, size), "WEBP")


# ── Fakes ─────────────────────────────────────────────────────────────────────

def _asset(asset_id: str, day: str = _DAY, **kw) -> Asset:
    fields = dict(id=asset_id, type="IMAGE", local_date_time=f"{day}T10:00:00.000Z",
                  file_created_at=f"{day}T08:00:00.000Z", lat=None, lon=None,
                  mime="image/jpeg", file_size=5_000_000, original_file_name="x.jpg",
                  is_trashed=False, is_archived=False)
    fields.update(kw)
    return Asset(**fields)


class _FakeImmich:
    def __init__(self):
        self.assets: list = []
        self.thumbs: dict = {}
        self.album_error: Exception | None = None
        self.thumb_calls: list = []
        self.windows: list = []

    def add(self, asset_id: str, thumb, day: str = _DAY, **kw) -> None:
        self.assets.append(_asset(asset_id, day, **kw))
        self.thumbs[asset_id] = thumb

    def iter_album_assets(self, album_id, taken_after, taken_before, *, max_items=10_000):
        self.windows.append((album_id, taken_after, taken_before))
        if self.album_error is not None:
            raise self.album_error
        yield from self.assets

    def thumbnail_bytes(self, asset_id, size):
        """A list answers its items in turn, then its last one for good."""
        assert size == "thumbnail"
        self.thumb_calls.append(asset_id)
        value = self.thumbs[asset_id]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        return value


class _Broker:
    """Stands in for RQ: records each enqueued job, runs them on drain."""

    def __init__(self):
        self.jobs: list = []
        self.ran: list = []

    def enqueue(self, queue_name, func, *args, **kwargs):
        assert queue_name == "photos"
        assert kwargs.get("allow_inline") is False
        self.jobs.append((func, args))
        return True

    def run_one(self) -> str:
        func, args = self.jobs.pop(0)
        self.ran.append(func.__name__)
        func(*args)
        return func.__name__

    def drain(self) -> list:
        while self.jobs:
            self.run_one()
        return self.ran


# ── Environment ───────────────────────────────────────────────────────────────

class _Env:
    def __init__(self, engine, tmp_path: Path, immich: _FakeImmich):
        self.engine = engine
        self.tmp = tmp_path
        self.immich = immich
        with Session(engine) as sess:
            owner = UserInfo(display_name="Owner", email="owner@example.com")
            mate = UserInfo(display_name="Mate", email="mate@example.com")
            sess.add_all([owner, mate])
            sess.commit()
            project = DBProject(user_info_id=owner.id, name="Trip")
            sess.add(project)
            sess.commit()
            sess.add(DBProjectMember(project_id=project.id, user_info_id=mate.id,
                                     role="editor", invited_by=owner.id))
            for uid in (owner.id, mate.id):
                sess.add(ImmichToken(user_info_id=uid, server_url="https://immich.example",
                                     api_key="secret-key"))
            sess.commit()
            self.owner, self.mate, self.project = owner.id, mate.id, project.id

    def memory(self, seeds, day: str = _DAY, *, files: bool = True, hashes=None,
               size=(400, 300)) -> tuple:
        """A memory whose photos are *seeds*' patterns; (id, [uuid, ...])."""
        uuids = [str(uuid_lib.uuid4()) for _ in seeds]
        state = load_state(None)
        if hashes:
            state["hashes"] = {uuids[i]: h for i, h in hashes.items()}
        with Session(self.engine) as sess:
            row = DBMemory(project_id=self.project, date=day, photos_json=json.dumps(uuids),
                           photo_order_json=dump_state(state))
            sess.add(row)
            sess.commit()
            mid = row.id
        if files:
            self._write("memories", self.owner, mid, uuids, seeds, size)
        return mid, uuids

    def journal(self, seeds, author, day: str = _DAY) -> tuple:
        uuids = [str(uuid_lib.uuid4()) for _ in seeds]
        with Session(self.engine) as sess:
            row = DBJournalEntry(project_id=self.project, user_info_id=author, date=day,
                                 photos_json=json.dumps(uuids))
            sess.add(row)
            sess.commit()
            jid = row.id
        self._write("journal", author if author is not None else self.owner, jid, uuids, seeds)
        return jid, uuids

    def _write(self, kind, uid, artifact_id, uuids, seeds, size=(400, 300)):
        folder = self.tmp / "users" / str(uid) / kind / str(artifact_id)
        folder.mkdir(parents=True, exist_ok=True)
        for name, seed in zip(uuids, seeds):
            (folder / f"{name}.jpg").write_bytes(_encode(_pattern(seed, size), "JPEG"))
            (folder / f"{name}_thumb.jpg").write_bytes(
                _encode(_pattern(seed, (size[0] // 2, size[1] // 2)), "JPEG"))

    def scan(self, user=None, *, status="pending", include_memories=True,
             include_journal=True, created_at=None, apply_status="idle") -> int:
        with Session(self.engine) as sess:
            row = DBPhotoUpgradeScan(
                project_id=self.project, user_info_id=user or self.owner, album_id="album",
                include_memories=include_memories, include_journal=include_journal,
                status=status, apply_status=apply_status,
                created_at=created_at if created_at is not None else time.time())
            sess.add(row)
            sess.commit()
            return row.id

    def reject(self, scan_id, kind, artifact_id, photo_uuid, asset_id) -> None:
        with Session(self.engine) as sess:
            sess.add(DBPhotoUpgradeSuggestion(
                scan_id=scan_id, artifact_kind=kind, artifact_id=artifact_id,
                photo_uuid=photo_uuid, position=0, date=_DAY, state="rejected",
                asset_id=asset_id))
            sess.commit()

    def get(self, scan_id):
        with Session(self.engine) as sess:
            return sess.get(DBPhotoUpgradeScan, scan_id)

    def suggestions(self, scan_id) -> list:
        with Session(self.engine) as sess:
            return list(sess.exec(select(DBPhotoUpgradeSuggestion).where(
                DBPhotoUpgradeSuggestion.scan_id == scan_id)
                .order_by(DBPhotoUpgradeSuggestion.artifact_kind,
                          DBPhotoUpgradeSuggestion.artifact_id,
                          DBPhotoUpgradeSuggestion.position)).all())

    def assets(self, scan_id) -> list:
        with Session(self.engine) as sess:
            return list(sess.exec(select(DBPhotoUpgradeAsset).where(
                DBPhotoUpgradeAsset.scan_id == scan_id)).all())


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'scan.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)
    immich = _FakeImmich()
    keys = []

    def make_client(server_url, api_key):
        keys.append((server_url, api_key))
        return immich

    monkeypatch.setattr(scan_job, "_make_client", make_client)
    monkeypatch.setattr(scan_job, "queue_available", lambda: False)
    monkeypatch.setattr(scan_job, "_RETRY_PAUSES", (0.0, 0.0))
    e = _Env(engine, tmp_path, immich)
    e.keys = keys
    yield e
    engine.dispose()


@pytest.fixture
def broker(monkeypatch):
    b = _Broker()
    monkeypatch.setattr(scan_job, "queue_available", lambda: True)
    monkeypatch.setattr(scan_job, "enqueue", b.enqueue)
    return b


# ── The chain ─────────────────────────────────────────────────────────────────

def test_a_600_photo_album_takes_three_fingerprint_jobs(env, broker):
    env.memory([1])
    thumb = _album_thumb(99)
    for i in range(600):
        env.immich.add(f"a{i:03d}", thumb)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    assert ran == ["run_scan_prepare", "run_scan_fingerprint", "run_scan_fingerprint",
                   "run_scan_fingerprint", "run_scan_match"]
    scan = env.get(scan_id)
    assert scan.status == "ready", scan.error_message
    assert (scan.progress_done, scan.progress_total) == (1, 1)
    rows = env.assets(scan_id)
    assert len(rows) == 600 and all(r.fingerprinted for r in rows)
    assert len(env.immich.thumb_calls) == 600
    # The runner's own key, decrypted.
    assert env.keys[0] == ("https://immich.example", "secret-key")


def test_progress_is_kept_between_fingerprint_jobs(env, broker):
    env.memory([1])
    for i in range(300):
        env.immich.add(f"a{i:03d}", _album_thumb(99))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.run_one()   # prepare
    assert (env.get(scan_id).stage, env.get(scan_id).progress_total) == ("fingerprinting", 300)
    broker.run_one()   # first batch
    scan = env.get(scan_id)
    assert (scan.progress_done, scan.progress_total) == (250, 300)
    assert scan.heartbeat_at is not None


def test_suggestions_follow_confidence_and_trip_order(env, broker):
    mid, uuids = env.memory([10, 11, 12])
    env.immich.add("exact", _album_thumb(10))
    env.immich.add("burst-1", _album_thumb(11))
    env.immich.add("burst-2", _album_thumb(11))
    env.immich.add("other", _album_thumb(50))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    rows = env.suggestions(scan_id)
    assert [(r.photo_uuid, r.position) for r in rows] == list(zip(uuids, range(3)))
    exact, burst, nothing = rows
    assert (exact.state, exact.asset_id, exact.confidence, exact.selected) == (
        "proposed", "exact", "high", True)
    assert exact.score <= 4
    assert exact.asset_bytes == 5_000_000 and exact.asset_mime == "image/jpeg"
    assert (exact.current_width, exact.current_height) == (400, 300)
    assert exact.current_bytes == (env.tmp / "users" / str(env.owner) / "memories"
                                   / str(mid) / f"{uuids[0]}.jpg").stat().st_size
    assert (burst.state, burst.confidence, burst.selected) == ("proposed", "low", False)
    assert burst.asset_id in ("burst-1", "burst-2")
    assert [a["asset_id"] for a in json.loads(burst.alternatives_json)] == [
        {"burst-1": "burst-2", "burst-2": "burst-1"}[burst.asset_id]]
    assert (nothing.state, nothing.selected) == ("no_match", False)
    assert all(r.date == _DAY and r.artifact_kind == "memory" for r in rows)


def test_exact_copies_take_their_hash_from_the_photo_order_state(env, broker):
    digest = "ab" * 32
    env.memory([20], hashes={0: digest})
    env.memory([20], day="2026-05-03", hashes={0: digest})
    env.immich.add("orig", _album_thumb(20))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    rows = env.suggestions(scan_id)
    assert [(r.state, r.asset_id, r.confidence) for r in rows] == [
        ("proposed", "orig", "high")] * 2


def test_a_companion_scans_only_their_own_journal_entries(env, broker):
    env.memory([30])
    mine, mine_uuids = env.journal([31], env.mate)
    env.journal([32], env.owner)
    env.journal([33], None)          # a NULL author is the owner
    for seed in (30, 31, 32, 33):
        env.immich.add(f"s{seed}", _album_thumb(seed))
    scan_id = env.scan(env.mate, include_memories=False)

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    rows = env.suggestions(scan_id)
    assert [(r.artifact_kind, r.artifact_id, r.photo_uuid) for r in rows] == [
        ("journal", mine, mine_uuids[0])]
    assert (rows[0].state, rows[0].asset_id) == ("proposed", "s31")


def test_a_companion_asking_for_memories_fails(env, broker):
    env.memory([30])
    scan_id = env.scan(env.mate, include_memories=True)

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "failed"
    assert env.get(scan_id).error_message == scan_job.REASON_NOT_OWNER


def test_the_owner_scan_covers_null_author_entries(env, broker):
    _, owner_uuids = env.journal([40], None)
    env.journal([41], env.mate)
    scan_id = env.scan(include_memories=False)

    scan_job.start_scan(scan_id)
    broker.drain()

    assert [r.photo_uuid for r in env.suggestions(scan_id)] == owner_uuids


def test_rejected_pairs_are_carried_over_from_an_earlier_scan(env, broker):
    mid, uuids = env.memory([50, 51])
    env.immich.add("a50", _album_thumb(50))
    env.immich.add("a51", _album_thumb(51))
    earlier = env.scan(status="ready", created_at=time.time() - 60)
    env.reject(earlier, "memory", mid, uuids[0], "a50")
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    first, second = env.suggestions(scan_id)
    assert first.state == "rejected" and first.asset_id == "a50" and not first.selected
    assert (second.state, second.asset_id) == ("proposed", "a51")
    assert env.get(earlier) is None

    # And on to the scan after: the rejection is still read.
    third = env.scan()
    scan_job.start_scan(third)
    broker.drain()
    assert env.suggestions(third)[0].state == "rejected"


def _reject_in_review(env, scan_id, photo_uuid):
    """What the review does to a suggestion the user turns down: its state."""
    with Session(env.engine) as sess:
        row = sess.exec(select(DBPhotoUpgradeSuggestion).where(
            DBPhotoUpgradeSuggestion.scan_id == scan_id,
            DBPhotoUpgradeSuggestion.photo_uuid == photo_uuid)).one()
        row.state, row.selected = "rejected", False
        sess.add(row)
        sess.commit()
        return row.asset_id


def _scan_once(env, broker) -> int:
    scan_id = env.scan()
    scan_job.start_scan(scan_id)
    broker.drain()
    assert env.get(scan_id).status == "ready", env.get(scan_id).error_message
    return scan_id


def test_a_rejected_frame_never_comes_back_once_a_sibling_is_proposed(env, broker):
    """U9-R1-1: reject A, a rescan proposes its burst sibling B and deletes
    the scan holding the rejection, and the scan after must still not
    propose A."""
    mid, (uuid,) = env.memory([110])
    env.immich.add("frame-a", _album_thumb(110))
    env.immich.add("frame-b", _album_thumb(110))
    key = scan_job.photo_key("memory", mid, uuid)

    first = _scan_once(env, broker)
    rejected = _reject_in_review(env, first, uuid)
    sibling = {"frame-a": "frame-b", "frame-b": "frame-a"}[rejected]

    second = _scan_once(env, broker)
    (row,) = env.suggestions(second)
    assert (row.state, row.asset_id) == ("proposed", sibling)
    # The rejected twin scores as well: the sibling is not ticked.
    assert (row.confidence, row.selected) == ("low", False)
    assert env.get(first) is None

    third = _scan_once(env, broker)
    (row,) = env.suggestions(third)
    assert row.asset_id == sibling and row.state == "proposed"
    assert rejected not in [a["asset_id"] for a in json.loads(row.alternatives_json)]
    assert [key, rejected] in json.loads(env.get(third).rejected_pairs_json)


def test_carried_rejections_survive_the_deletion_of_older_scans(env, broker):
    mid, uuids = env.memory([111, 112])
    env.immich.add("a111", _album_thumb(111))
    older = env.scan(status="ready", created_at=time.time() - 120,
                     )
    env.reject(older, "memory", mid, uuids[0], "a111")
    with Session(env.engine) as sess:
        row = sess.get(DBPhotoUpgradeScan, older)
        row.rejected_pairs_json = json.dumps([[scan_job.photo_key("memory", mid, uuids[1]),
                                               "gone-asset"]])
        sess.add(row)
        sess.commit()

    newer = _scan_once(env, broker)

    assert env.get(older) is None
    carried = {tuple(p) for p in json.loads(env.get(newer).rejected_pairs_json)}
    assert carried == {(scan_job.photo_key("memory", mid, uuids[0]), "a111"),
                       (scan_job.photo_key("memory", mid, uuids[1]), "gone-asset")}
    assert env.suggestions(newer)[0].state == "rejected"


def test_a_failed_scan_copies_no_rejections_and_deletes_nothing(env, broker):
    mid, uuids = env.memory([113])
    older = env.scan(status="ready", created_at=time.time() - 60)
    env.reject(older, "memory", mid, uuids[0], "a113")
    env.immich.album_error = ImmichUnreachable("down")
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "failed"
    assert json.loads(env.get(scan_id).rejected_pairs_json) == []
    assert env.get(older) is not None


def test_record_rejection_adds_each_pair_once(env):
    scan_id = env.scan(status="ready")
    assert scan_job.record_rejection(scan_id, "memory:1:u", "a1")
    assert scan_job.record_rejection(scan_id, "memory:1:u", "a1")
    assert scan_job.record_rejection(scan_id, "memory:1:u", "a2")
    assert json.loads(env.get(scan_id).rejected_pairs_json) == [
        ["memory:1:u", "a1"], ["memory:1:u", "a2"]]
    assert not scan_job.record_rejection(scan_id + 99, "memory:1:u", "a1")


def test_a_rejection_recorded_on_the_scan_is_honoured_by_the_next(env, broker):
    """A pair only in the column (say an alternative turned down in the
    chooser while the row proposes another asset) still bars the asset."""
    mid, (uuid,) = env.memory([114])
    env.immich.add("a114", _album_thumb(114))
    env.immich.add("b114", _album_thumb(114))
    first = _scan_once(env, broker)
    (row,) = env.suggestions(first)
    other = {"a114": "b114", "b114": "a114"}[row.asset_id]
    scan_job.record_rejection(first, scan_job.photo_key("memory", mid, uuid), other)

    second = _scan_once(env, broker)

    (row,) = env.suggestions(second)
    assert row.asset_id != other and row.state == "proposed"


# ── Reversed rejections and late ones (U9-R2-1, U9-R2-2) ─────────────────────

def _set_row(env, scan_id, photo_uuid, **values):
    with Session(env.engine) as sess:
        row = sess.exec(select(DBPhotoUpgradeSuggestion).where(
            DBPhotoUpgradeSuggestion.scan_id == scan_id,
            DBPhotoUpgradeSuggestion.photo_uuid == photo_uuid)).one()
        for name, value in values.items():
            setattr(row, name, value)
        sess.add(row)
        sess.commit()


def _pairs(env, scan_id) -> set:
    return {tuple(p) for p in json.loads(env.get(scan_id).rejected_pairs_json)}


def test_an_un_rejected_frame_can_be_proposed_again(env, broker):
    mid, (uuid,) = env.memory([120])
    env.immich.add("frame-a", _album_thumb(120))
    env.immich.add("frame-b", _album_thumb(120))
    key = scan_job.photo_key("memory", mid, uuid)
    first = _scan_once(env, broker)
    rejected = _reject_in_review(env, first, uuid)
    second = _scan_once(env, broker)
    assert (key, rejected) in _pairs(env, second)
    assert env.suggestions(second)[0].asset_id != rejected

    # The review un-rejects it on the second scan.
    assert scan_job.remove_rejection(second, key, rejected)
    third = _scan_once(env, broker)

    (row,) = env.suggestions(third)
    offered = {row.asset_id} | {a["asset_id"] for a in json.loads(row.alternatives_json)}
    assert rejected in offered
    assert (key, rejected) not in _pairs(env, third)


def test_choosing_a_rejected_frame_lifts_its_rejection(env, broker):
    mid, (uuid,) = env.memory([121])
    env.immich.add("frame-a", _album_thumb(121))
    key = scan_job.photo_key("memory", mid, uuid)
    first = _scan_once(env, broker)
    rejected = _reject_in_review(env, first, uuid)
    second = _scan_once(env, broker)
    (row,) = env.suggestions(second)
    assert (row.state, row.asset_id) == ("rejected", rejected)

    # "Choose another" picks it back for that photo; the column is left as
    # it was: the row decides.
    _set_row(env, second, uuid, state="proposed", asset_id=rejected, selected=True)
    third = _scan_once(env, broker)

    (row,) = env.suggestions(third)
    assert (row.state, row.asset_id) == ("proposed", rejected)
    assert (key, rejected) not in _pairs(env, third)


def test_the_newest_earlier_scan_decides(env):
    """Two earlier scans kept (their applies are running) disagree on one
    pair: the newer one's opinion wins, either way."""
    key = "memory:1:00000000-0000-4000-8000-000000000001"

    def scans(newer_rejects: bool) -> int:
        older = env.scan(status="ready", created_at=time.time() - 200, apply_status="running")
        newer = env.scan(status="ready", created_at=time.time() - 100, apply_status="running")
        allowing, rejecting = (older, newer) if newer_rejects else (newer, older)
        with Session(env.engine) as sess:
            sess.add(DBPhotoUpgradeSuggestion(
                scan_id=allowing, artifact_kind="memory", artifact_id=1,
                photo_uuid=key.split(":")[2], position=0, date=_DAY,
                state="proposed", asset_id="A"))
            sess.commit()
        scan_job.record_rejection(rejecting, key, "A")
        return env.scan(created_at=time.time())

    current = scans(newer_rejects=True)
    with Session(env.engine) as sess:
        assert scan_job._earlier_rejections(sess, sess.get(DBPhotoUpgradeScan, current)) == {
            key: ["A"]}
        delete_photo_upgrade_scans(sess, DBPhotoUpgradeScan.id > 0)
        sess.commit()

    current = scans(newer_rejects=False)
    with Session(env.engine) as sess:
        assert scan_job._earlier_rejections(sess, sess.get(DBPhotoUpgradeScan, current)) == {}


def test_record_and_remove_touch_only_their_own_pair(env):
    scan_id = env.scan(status="ready")
    for pair in (("memory:1:u", "a1"), ("memory:1:u", "a2"), ("journal:2:v", "a1")):
        scan_job.record_rejection(scan_id, *pair)

    assert scan_job.remove_rejection(scan_id, "memory:1:u", "a1")
    assert _pairs(env, scan_id) == {("memory:1:u", "a2"), ("journal:2:v", "a1")}
    assert scan_job.remove_rejection(scan_id, "memory:1:u", "absent")
    assert _pairs(env, scan_id) == {("memory:1:u", "a2"), ("journal:2:v", "a1")}
    scan_job.record_rejection(scan_id, "memory:1:u", "a1")
    assert _pairs(env, scan_id) == {("memory:1:u", "a1"), ("memory:1:u", "a2"),
                                    ("journal:2:v", "a1")}
    assert not scan_job.remove_rejection(scan_id + 99, "memory:1:u", "a1")


def test_a_rejection_made_during_the_match_is_carried(env, broker, monkeypatch):
    """U9-R2-1: a rejection written to the earlier scan after the match read
    the rejections, before the earlier scan is deleted."""
    mid, (uuid,) = env.memory([122])
    env.immich.add("frame-a", _album_thumb(122))
    key = scan_job.photo_key("memory", mid, uuid)
    first = _scan_once(env, broker)
    (row,) = env.suggestions(first)
    assert (row.state, row.asset_id, row.selected) == ("proposed", "frame-a", True)
    real_match = scan_job.match

    def match_then_the_user_rejects(*args, **kwargs):
        result = real_match(*args, **kwargs)
        # What the review does on the first scan, while the second matches.
        _reject_in_review(env, first, uuid)
        scan_job.record_rejection(first, key, "frame-a")
        return result

    monkeypatch.setattr(scan_job, "match", match_then_the_user_rejects)
    second = _scan_once(env, broker)

    assert env.get(first) is None
    assert (key, "frame-a") in _pairs(env, second)
    (row,) = env.suggestions(second)
    assert (row.state, row.asset_id, row.selected) == ("rejected", "frame-a", False)

    monkeypatch.setattr(scan_job, "match", real_match)
    third = _scan_once(env, broker)
    assert env.suggestions(third)[0].state == "rejected"


def test_a_late_rejection_leaves_other_proposals_alone(env, broker, monkeypatch):
    mid, uuids = env.memory([123, 124])
    env.immich.add("a123", _album_thumb(123))
    env.immich.add("a124", _album_thumb(124))
    first = _scan_once(env, broker)
    real_match = scan_job.match

    def match_then_the_user_rejects(*args, **kwargs):
        result = real_match(*args, **kwargs)
        _reject_in_review(env, first, uuids[0])
        return result

    monkeypatch.setattr(scan_job, "match", match_then_the_user_rejects)
    second = _scan_once(env, broker)

    rejected, kept = env.suggestions(second)
    assert (rejected.state, rejected.selected) == ("rejected", False)
    assert (kept.state, kept.asset_id, kept.selected) == ("proposed", "a124", True)


def test_earlier_scans_are_deleted_only_once_the_new_one_is_ready(env, broker):
    env.memory([60])
    env.immich.add("a60", _album_thumb(60))
    earlier = env.scan(status="ready", created_at=time.time() - 60)
    failed = env.scan(status="failed", created_at=time.time() - 30)
    other_person = env.scan(env.mate, status="ready", include_memories=False)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    while broker.jobs:
        name = broker.run_one()
        if name != "run_scan_match":
            assert env.get(earlier) is not None and env.get(failed) is not None

    assert env.get(scan_id).status == "ready"
    assert env.get(earlier) is None and env.get(failed) is None
    assert env.get(other_person) is not None


@pytest.mark.parametrize("apply_status", ["pending", "running"])
def test_an_earlier_scan_with_an_apply_in_progress_is_kept(env, broker, apply_status):
    env.memory([61])
    busy = env.scan(status="ready", created_at=time.time() - 60, apply_status=apply_status)
    done = env.scan(status="ready", created_at=time.time() - 30, apply_status="done")
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    assert env.get(busy) is not None
    assert env.get(done) is None


def test_an_apply_starting_during_the_match_is_honoured(env, broker, monkeypatch):
    """The condition is in the DELETE: an apply that starts after the match
    began keeps its scan."""
    env.memory([62])
    earlier = env.scan(status="ready", created_at=time.time() - 60)
    real_match = scan_job.match

    def match_while_an_apply_starts(*args, **kwargs):
        with Session(env.engine) as sess:
            row = sess.get(DBPhotoUpgradeScan, earlier)
            row.apply_status = "pending"
            sess.add(row)
            sess.commit()
        return real_match(*args, **kwargs)

    monkeypatch.setattr(scan_job, "match", match_while_an_apply_starts)
    scan_id = env.scan()
    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    assert env.get(earlier) is not None


def test_a_failed_scan_leaves_the_earlier_scan_and_its_rejections(env, broker):
    mid, uuids = env.memory([70])
    earlier = env.scan(status="ready", created_at=time.time() - 60)
    env.reject(earlier, "memory", mid, uuids[0], "a70")
    env.immich.album_error = ImmichUnreachable("down")
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "failed"
    assert env.get(earlier).status == "ready"
    assert [(r.state, r.asset_id) for r in env.suggestions(earlier)] == [("rejected", "a70")]


# ── The original's size ───────────────────────────────────────────────────────

def test_an_album_photo_with_no_more_pixels_is_already_original(env, broker):
    env.memory([100, 101])     # stored at 400x300
    env.immich.add("same", _album_thumb(100), width=400, height=300)
    env.immich.add("bigger", _album_thumb(101), width=4080, height=3072)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    same, bigger = env.suggestions(scan_id)
    assert (same.state, same.asset_id, same.selected) == ("already_original", "same", False)
    assert (bigger.state, bigger.asset_id, bigger.selected) == ("proposed", "bigger", True)
    assert (bigger.asset_width, bigger.asset_height) == (4080, 3072)
    stored = {r.asset_id: r for r in env.assets(scan_id)}
    assert (stored["bigger"].original_width, stored["bigger"].original_height) == (4080, 3072)
    # The thumbnail's upright size stays for the aspect gate.
    assert (stored["bigger"].width, stored["bigger"].height) == (332, 249)


def test_a_portrait_thumbnail_turns_a_landscape_exif_pair_upright(env, broker):
    env.memory([102], size=(300, 400))
    env.immich.add("portrait", _album_thumb(102, (249, 332)), width=4080, height=3072)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    (row,) = env.suggestions(scan_id)
    assert (row.state, row.asset_id) == ("proposed", "portrait")
    assert (row.asset_width, row.asset_height) == (3072, 4080)
    stored = env.assets(scan_id)[0]
    assert (stored.original_width, stored.original_height) == (4080, 3072)


def test_without_exif_sizes_the_dimensions_stay_unknown(env, broker):
    # Same pixels as the stored photo would be "already original" if known.
    env.memory([103], size=(332, 249))
    env.immich.add("no-exif", _album_thumb(103))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    (row,) = env.suggestions(scan_id)
    assert (row.state, row.asset_id) == ("proposed", "no-exif")
    assert (row.asset_width, row.asset_height) == (None, None)


# ── Failures ──────────────────────────────────────────────────────────────────

def test_too_many_album_photos_fail_the_scan(env, broker):
    env.memory([80])
    env.immich.album_error = ImmichTooManyAssets(10_000)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "failed"
    assert "10,000" in scan.error_message
    assert broker.ran == ["run_scan_prepare"]
    assert env.assets(scan_id) == []


def test_more_than_5000_trip_photos_fail_the_scan(env, broker):
    env.memory(list(range(scan_job.MAX_TRIP_PHOTOS)), files=False)
    env.memory([1], files=False)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "failed"
    assert scan.error_message == scan_job.REASON_TOO_MANY_TRIP_PHOTOS
    assert "5,000" in scan.error_message
    assert env.immich.windows == []   # failed before asking Immich


def test_exactly_5000_trip_photos_are_allowed(env, broker):
    env.memory(list(range(scan_job.MAX_TRIP_PHOTOS)), files=False)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    assert len(env.suggestions(scan_id)) == scan_job.MAX_TRIP_PHOTOS


def test_an_unreachable_immich_fails_the_scan(env, broker):
    env.memory([81])
    env.immich.album_error = ImmichUnreachable("connection refused to https://immich.example")
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert (scan.status, scan.error_message) == ("failed", scan_job.REASON_UNREACHABLE)
    assert "immich.example" not in scan.error_message


def test_immich_going_down_during_fingerprinting_fails_the_scan(env, broker):
    env.memory([82])
    for i in range(3):
        env.immich.add(f"a{i}", ImmichUnreachable("down"))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).error_message == scan_job.REASON_UNREACHABLE


@pytest.mark.parametrize("where", ["album", "thumbnail"])
def test_a_403_fails_the_scan_naming_the_permissions(env, broker, where):
    env.memory([83])
    if where == "album":
        env.immich.album_error = ImmichRejected(403)
    else:
        env.immich.add("ok", _album_thumb(83))
        env.immich.add("refused", ImmichRejected(403))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "failed"
    assert scan.error_message == scan_job.REASON_FORBIDDEN
    for permission in ("album.read", "asset.read", "asset.view", "asset.download"):
        assert permission in scan.error_message


def test_a_failed_thumbnail_fetch_is_counted_and_the_scan_goes_on(env, broker):
    env.memory([84])
    env.immich.add("good", _album_thumb(84))
    env.immich.add("missing", ImmichRejected(404))
    env.immich.add("broken", b"not an image")
    env.immich.add("flaky", ImmichUnreachable("reset"))
    # Untrusted: a "thumbnail" far larger than one is never decoded.
    env.immich.add("huge", _encode(Image.new("L", (2500, 2000)), "PNG"))
    # Enough of the album read that the four left out are a result, not
    # Immich down (INT-R3-1).
    _readable(env, _under_the_share(4) - 5)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "ready", scan.error_message
    rows = {r.asset_id: r for r in env.assets(scan_id)}
    assert {a for a, r in rows.items() if r.fetch_failed} == {"missing", "broken", "flaky", "huge"}
    assert rows["good"].fingerprinted and not rows["good"].fetch_failed
    assert env.suggestions(scan_id)[0].asset_id == "good"


# ── Failed fetches tried again (INT-R1-A1) ────────────────────────────────────

def _calls(env) -> dict:
    return {a: env.immich.thumb_calls.count(a) for a in set(env.immich.thumb_calls)}


def _named_calls(env) -> dict:
    """:func:`_calls` without the padding :func:`_readable` adds."""
    return {a: n for a, n in _calls(env).items() if not a.startswith("ok-")}


def test_a_fetch_that_can_pass_is_tried_again_and_matched(env, broker, monkeypatch):
    pauses = []
    monkeypatch.setattr(scan_job, "_RETRY_PAUSES", (2.0, 5.0))
    monkeypatch.setattr(scan_job.time, "sleep", pauses.append)
    mid, uuids = env.memory([120, 121, 122, 123])
    env.immich.add("good", _album_thumb(120))
    env.immich.add("server-error", [ImmichRejected(503), _album_thumb(121)])
    env.immich.add("timed-out", [ImmichUnreachable("read timed out"),
                                 ImmichUnreachable("read timed out"), _album_thumb(122)])
    env.immich.add("cut-off", [b"<html>502 Bad Gateway</html>", _album_thumb(123)])
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "ready", scan.error_message
    assert ran == ["run_scan_prepare", "run_scan_fingerprint", "run_scan_fingerprint",
                   "run_scan_fingerprint", "run_scan_match"]
    assert pauses == [2.0, 5.0]
    assert _calls(env) == {"good": 1, "server-error": 2, "timed-out": 3, "cut-off": 2}
    rows = env.assets(scan_id)
    assert all(r.fingerprinted and not r.fetch_failed for r in rows)
    assert [r.asset_id for r in env.suggestions(scan_id)] == [
        "good", "server-error", "timed-out", "cut-off"]


def test_a_permanent_failure_is_not_fetched_again(env, broker):
    env.memory([124])
    env.immich.add("good", _album_thumb(124))
    env.immich.add("gone", ImmichRejected(404))
    env.immich.add("refused", ImmichRejected(400))
    env.immich.add("over-the-byte-cap", ImmichBadAnswer("thumbnail larger than allowed"))
    env.immich.add("over-the-pixel-cap", _encode(Image.new("L", (2500, 2000)), "PNG"))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    assert env.get(scan_id).status == "ready"
    assert ran == ["run_scan_prepare", "run_scan_fingerprint", "run_scan_match"]
    assert _calls(env) == {"good": 1, "gone": 1, "refused": 1, "over-the-byte-cap": 1,
                           "over-the-pixel-cap": 1}
    rows = {r.asset_id: r for r in env.assets(scan_id)}
    assert {a for a, r in rows.items() if r.fetch_failed} == {
        "gone", "refused", "over-the-byte-cap", "over-the-pixel-cap"}


def test_a_photo_failing_every_attempt_is_given_up_and_the_scan_is_ready(env, broker):
    env.memory([125, 126])
    env.immich.add("good", _album_thumb(125))
    env.immich.add("always-down", ImmichUnreachable("reset"))
    env.immich.add("always-5xx", ImmichRejected(502))
    env.immich.add("always-429", ImmichRejected(429))
    # Enough of the album read that the three given up are a result, not
    # Immich down (INT-R3-1).
    _readable(env, _under_the_share(3) - 4)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "ready", scan.error_message
    tries = 1 + len(scan_job._RETRY_PAUSES)
    assert _named_calls(env) == {"good": 1, "always-down": tries, "always-5xx": tries,
                           "always-429": tries}
    rows = {r.asset_id: r for r in env.assets(scan_id)}
    assert {a for a, r in rows.items() if r.fetch_failed} == {
        "always-down", "always-5xx", "always-429"}
    assert (scan.progress_done, scan.progress_total) == (2, 2)
    assert [r.state for r in env.suggestions(scan_id)] == ["proposed", "no_match"]


def test_every_pass_walks_its_rows_in_batches(env, broker, monkeypatch):
    """A pass longer than one batch carries on after the last row it read,
    so a photo waiting for the next pass is not fetched twice in this one."""
    monkeypatch.setattr(scan_job, "FINGERPRINT_BATCH", 2)
    env.memory([127])
    # Read first, so the one photo given up is a result, not Immich down
    # (INT-R3-1).
    padding = _under_the_share(1) - 5
    _readable(env, padding)
    env.immich.add("a", _album_thumb(127))
    env.immich.add("b", [ImmichRejected(500), _album_thumb(128)])
    env.immich.add("c", ImmichRejected(500))
    env.immich.add("d", _album_thumb(129))
    # Alone in its first-pass batch, but photos of the scan got through
    # before it: no sign that Immich is down either way.
    env.immich.add("e", [ImmichRejected(503), ImmichUnreachable("x"), _album_thumb(130)])
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    assert env.get(scan_id).status == "ready"
    # Pass 0: the padding, two a batch | a b | c d | e. Pass 1: b c | e.
    # Pass 2: c e.
    assert padding % 2 == 0
    assert ran.count("run_scan_fingerprint") == padding // 2 + 3 + 2 + 1
    assert _named_calls(env) == {"a": 1, "b": 2, "c": 3, "d": 1, "e": 3}
    rows = {r.asset_id: r for r in env.assets(scan_id)}
    assert {a for a, r in rows.items() if r.fingerprinted} == {
        "a", "b", "d", "e"} | {a for a in rows if a.startswith("ok-")}
    assert {a for a, r in rows.items() if r.fetch_failed} == {"c"}


def test_without_a_failure_no_pass_is_paused_for(env, broker, monkeypatch):
    pauses = []
    monkeypatch.setattr(scan_job.time, "sleep", pauses.append)
    env.memory([131])
    env.immich.add("good", _album_thumb(131))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    assert pauses == []


@pytest.mark.parametrize("error, reason", [
    (ImmichRejected(401), scan_job.REASON_FORBIDDEN),
    (ImmichRejected(403), scan_job.REASON_FORBIDDEN),
    (ImmichAddressNotAllowed("private"), scan_job.REASON_ADDRESS_NOT_ALLOWED),
])
def test_a_refusal_on_a_later_pass_still_fails_the_scan(env, broker, error, reason):
    env.memory([132])
    env.immich.add("good", _album_thumb(132))
    env.immich.add("flaky", [ImmichUnreachable("reset"), error])
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert (scan.status, scan.error_message) == ("failed", reason)


def test_a_later_pass_with_immich_unreachable_does_not_fail_the_scan(env, broker):
    """Only a first-pass batch says Immich is down: a later pass holds only
    the photos that failed before."""
    env.memory([133])
    env.immich.add("good", _album_thumb(133))
    env.immich.add("flaky", ImmichUnreachable("reset"))
    # Enough of the album read that the one given up is a result, not
    # Immich down (INT-R3-1).
    _readable(env, _under_the_share(1) - 2)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    assert _calls(env)["flaky"] == 1 + len(scan_job._RETRY_PAUSES)


def test_a_first_pass_with_one_photo_unreachable_still_fails_the_scan(env, broker):
    env.memory([134])
    env.immich.add("only", ImmichUnreachable("refused"))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).error_message == scan_job.REASON_UNREACHABLE
    assert _calls(env) == {"only": 1}


# ── Immich down, judged over the whole scan (INT-R2-1, INT-R2-2) ─────────────

def _reviewed_scan(env, broker) -> tuple:
    """A ready scan of two trip photos, with a tick on the first and a
    "Choose another" pick on the second; (scan id, its rows as reviewed)."""
    mid, uuids = env.memory([140, 141])
    env.immich.add("a140", _album_thumb(140))
    env.immich.add("a141", _album_thumb(141))
    earlier = _scan_once(env, broker)
    ticked, other = env.suggestions(earlier)
    assert (ticked.asset_id, ticked.selected) == ("a140", True)
    _set_row(env, earlier, other.photo_uuid, state="proposed", asset_id="a140",
             selected=True)
    broker.ran.clear()
    env.immich.thumb_calls.clear()
    return earlier, _review(env, earlier)


def _review(env, scan_id) -> list:
    return [(r.photo_uuid, r.state, r.asset_id, r.selected)
            for r in env.suggestions(scan_id)]


def _unreadable_photos(env, scan_id) -> int:
    """What the API reports for the scan."""
    from api.photo_upgrade import _scan_out
    with Session(env.engine) as sess:
        return _scan_out(sess, sess.get(DBPhotoUpgradeScan, scan_id))["unreadable_photos"]


def test_every_thumbnail_answering_503_fails_the_scan_and_keeps_the_review(env, broker):
    """INT-R2-1: Immich restarting behind its proxy answers 503, not a
    refused connection; the first batch still says Immich is down."""
    earlier, reviewed = _reviewed_scan(env, broker)
    for asset_id in ("a140", "a141"):
        env.immich.thumbs[asset_id] = ImmichRejected(503)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    scan = env.get(scan_id)
    assert (scan.status, scan.error_message) == (
        "failed", "Your Immich server refused the request (HTTP 503).")
    assert "run_scan_match" not in ran
    assert _calls(env) == {"a140": 1, "a141": 1}
    assert env.get(earlier).status == "ready"
    assert _review(env, earlier) == reviewed


def test_nothing_read_after_every_pass_fails_the_scan_and_keeps_the_review(env, broker):
    """INT-R2-1: a 404 among the 502s keeps the first batch from saying
    Immich is down; the retry passes fail too, and nothing is matched."""
    earlier, reviewed = _reviewed_scan(env, broker)
    for asset_id in ("a140", "a141"):
        env.immich.thumbs[asset_id] = ImmichRejected(502)
    env.immich.add("gone", ImmichRejected(404))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    scan = env.get(scan_id)
    assert (scan.status, scan.error_message) == ("failed", scan_job.REASON_UNREACHABLE)
    assert "run_scan_match" not in ran
    tries = 1 + len(scan_job._RETRY_PAUSES)
    assert _calls(env) == {"a140": tries, "a141": tries, "gone": 1}
    assert env.get(earlier).status == "ready"
    assert _review(env, earlier) == reviewed


@pytest.mark.parametrize("passes_on_retry", [True, False])
def test_a_lone_last_photo_timing_out_does_not_fail_the_scan(env, broker, passes_on_retry):
    """INT-R2-2: after a full first batch got through, a one-photo last batch
    that times out is tried again, never a sign that Immich is down."""
    env.memory([142])
    thumb = _album_thumb(99)
    for i in range(scan_job.FINGERPRINT_BATCH):
        env.immich.add(f"a{i:03d}", thumb)
    timeout = ImmichUnreachable("read timed out")
    env.immich.add("late", [timeout, _album_thumb(142)] if passes_on_retry else timeout)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "ready", scan.error_message
    assert _calls(env)["late"] == (2 if passes_on_retry else 1 + len(scan_job._RETRY_PAUSES))
    rows = env.assets(scan_id)
    assert len(rows) == scan_job.FINGERPRINT_BATCH + 1
    if passes_on_retry:
        assert all(r.fingerprinted for r in rows)
        assert _unreadable_photos(env, scan_id) == 0
        assert env.suggestions(scan_id)[0].asset_id == "late"
    else:
        assert [r.asset_id for r in rows if r.fetch_failed] == ["late"]
        assert _unreadable_photos(env, scan_id) == 1


def test_every_asset_gone_still_makes_the_scan_ready(env, broker):
    """Every failure permanent (404) with nothing read is a result, not
    Immich down: the scan is ready, all "No match"."""
    env.memory([143, 144])
    env.immich.add("gone-1", ImmichRejected(404))
    env.immich.add("gone-2", ImmichRejected(404))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    scan = env.get(scan_id)
    assert scan.status == "ready", scan.error_message
    assert ran == ["run_scan_prepare", "run_scan_fingerprint", "run_scan_match"]
    assert _unreadable_photos(env, scan_id) == 2
    assert [r.state for r in env.suggestions(scan_id)] == ["no_match", "no_match"]


# ── Immich down partway through the scan (INT-R3-1, INT-R3-2) ────────────────

def _readable(env, count: int) -> None:
    """*count* album photos that read at once, none a match for a trip photo."""
    thumb = _album_thumb(99)
    for i in range(count):
        env.immich.add(f"ok-{len(env.immich.assets):04d}", thumb)


def _down(env, count: int, error) -> list:
    """*count* album photos whose every fetch fails with *error*; their ids."""
    ids = [f"down-{len(env.immich.assets) + i:04d}" for i in range(count)]
    for asset_id in ids:
        env.immich.add(asset_id, error)
    return ids


def _under_the_share(unread: int) -> int:
    """The fewest album photos of which *unread* is under the share at which
    a scan fails (``_UNREAD_FAIL_PERCENT``)."""
    return unread * 100 // scan_job._UNREAD_FAIL_PERCENT + 1


def _assert_failed_keeping(env, scan_id, ran, earlier, reviewed) -> None:
    scan = env.get(scan_id)
    assert (scan.status, scan.error_message) == ("failed", scan_job.REASON_UNREACHABLE)
    assert "run_scan_match" not in ran
    assert env.get(earlier).status == "ready"
    assert _review(env, earlier) == reviewed


@pytest.mark.parametrize("error", [ImmichUnreachable("connection refused"),
                                   ImmichRejected(503)])
def test_immich_down_after_the_first_batch_fails_the_scan_and_keeps_the_review(
        env, broker, error):
    """INT-R3-1: the first batch reads, then Immich stays down through every
    retry pass. The first-pass check no longer fires, since photos got
    through; the half of the album left unread fails the scan."""
    earlier, reviewed = _reviewed_scan(env, broker)
    _readable(env, scan_job.FINGERPRINT_BATCH - 2)
    down = _down(env, scan_job.FINGERPRINT_BATCH, error)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    _assert_failed_keeping(env, scan_id, ran, earlier, reviewed)
    tries = 1 + len(scan_job._RETRY_PAUSES)
    calls = _calls(env)
    assert {calls[a] for a in down} == {tries}
    assert (calls["a140"], calls["a141"]) == (1, 1)
    rows = env.assets(scan_id)
    assert sum(r.fingerprinted for r in rows) == scan_job.FINGERPRINT_BATCH
    assert {r.asset_id for r in rows if r.fetch_failed} == set(down)


def test_immich_down_partway_through_a_batch_fails_the_scan_and_keeps_the_review(
        env, broker, monkeypatch):
    """INT-R3-1: the outage starts inside a batch, after some of its photos
    and the whole batch before it got through."""
    monkeypatch.setattr(scan_job, "FINGERPRINT_BATCH", 10)
    earlier, reviewed = _reviewed_scan(env, broker)
    _readable(env, 8 + 4)
    down = _down(env, 6, ImmichUnreachable("reset"))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    _assert_failed_keeping(env, scan_id, ran, earlier, reviewed)
    rows = env.assets(scan_id)
    assert len(rows) == 20
    assert {r.asset_id for r in rows if r.fetch_failed} == set(down)


@pytest.mark.parametrize("at_the_share", [False, True])
def test_the_unread_share_decides_between_ready_and_failed(env, broker, at_the_share):
    """INT-R3-1: one photo under the share the scan goes ready and counts
    the photos it could not read; at the share it fails and keeps the
    earlier review."""
    album = 100
    share = album * scan_job._UNREAD_FAIL_PERCENT // 100
    unread = share if at_the_share else share - 1
    earlier, reviewed = _reviewed_scan(env, broker)
    _readable(env, album - 2 - unread)
    _down(env, unread, ImmichUnreachable("read timed out"))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    assert len(env.assets(scan_id)) == album
    if at_the_share:
        _assert_failed_keeping(env, scan_id, ran, earlier, reviewed)
    else:
        scan = env.get(scan_id)
        assert scan.status == "ready", scan.error_message
        assert _unreadable_photos(env, scan_id) == unread
        assert [r.asset_id for r in env.suggestions(scan_id)] == ["a140", "a141"]


def test_every_thumbnail_undecodable_fails_the_scan_and_keeps_the_review(env, broker):
    """INT-R3-2: a proxy answering 200 with an HTML page for every thumbnail
    is a failure that can pass (:func:`scan_job._can_pass`); after the retry
    passes nothing is read, so the scan fails as Immich unreachable and the
    earlier review is kept."""
    earlier, reviewed = _reviewed_scan(env, broker)
    for asset_id in ("a140", "a141"):
        env.immich.thumbs[asset_id] = b"<html>Sign in</html>"
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    ran = broker.drain()

    _assert_failed_keeping(env, scan_id, ran, earlier, reviewed)
    tries = 1 + len(scan_job._RETRY_PAUSES)
    assert _calls(env) == {"a140": tries, "a141": tries}


def test_a_killed_retry_pass_fails_its_scan(env):
    scan_id = env.scan(status="scanning")

    worker_mod._work_horse_killed_handler(
        _FakeRQJob((scan_job.run_scan_fingerprint, scan_id, 1, 40)), 123, 9, None)

    assert env.get(scan_id).error_message == scan_job.REASON_INTERRUPTED


def test_a_batch_never_holds_more_bodies_than_fetch_threads(env, broker, monkeypatch):
    """U9-R1-2: each thread hashes what it fetched before fetching again, so
    at most FETCH_THREADS thumbnail bodies are alive at once."""
    import threading

    env.memory([115])
    for i in range(40):
        env.immich.add(f"a{i:02d}", _album_thumb(115))
    lock = threading.Lock()
    alive = {"now": 0, "max": 0}
    real_thumbnail = env.immich.thumbnail_bytes
    real_fingerprint = scan_job._album_fingerprint

    def thumbnail(asset_id, size):
        raw = real_thumbnail(asset_id, size)
        with lock:
            alive["now"] += 1
            alive["max"] = max(alive["max"], alive["now"])
        time.sleep(0.005)
        return raw

    def album_fingerprint(raw):
        try:
            return real_fingerprint(raw)
        finally:
            with lock:
                alive["now"] -= 1

    monkeypatch.setattr(env.immich, "thumbnail_bytes", thumbnail)
    monkeypatch.setattr(scan_job, "_album_fingerprint", album_fingerprint)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).status == "ready"
    assert alive["now"] == 0
    assert 1 <= alive["max"] <= scan_job.FETCH_THREADS


def test_no_immich_connection_fails_the_scan(env, broker):
    env.memory([85])
    with Session(env.engine) as sess:
        for tok in sess.exec(select(ImmichToken)).all():
            sess.delete(tok)
        sess.commit()
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).error_message == scan_job.REASON_NOT_CONNECTED


def test_album_photos_outside_the_trip_trashed_or_archived_are_left_out(env, broker):
    env.memory([86], day="2026-05-02")
    env.immich.add("in", _album_thumb(86))
    env.immich.add("day-before", _album_thumb(86), day="2026-05-01")
    env.immich.add("two-days-after", _album_thumb(86), day="2026-05-04")
    env.immich.add("trashed", _album_thumb(86), is_trashed=True)
    env.immich.add("archived", _album_thumb(86), is_archived=True)
    env.immich.add("video", _album_thumb(86), type="VIDEO")
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert {r.asset_id for r in env.assets(scan_id)} == {"in", "day-before"}
    album, after, before = env.immich.windows[0]
    # Local days 05-01..05-03 for any time zone: UTC -14 h to +12 h around them.
    assert (album, after, before) == ("album", "2026-04-30T10:00:00.000Z",
                                      "2026-05-04T12:00:00.000Z")


def test_a_project_deleted_mid_chain_stops_the_next_job_quietly(env, broker):
    env.memory([87])
    env.immich.add("a87", _album_thumb(87))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.run_one()   # prepare
    with Session(env.engine) as sess:
        assert ProjectRepo().delete_project(sess, env.owner, "Trip")
        sess.commit()
    broker.drain()

    assert broker.ran == ["run_scan_prepare", "run_scan_fingerprint"]
    assert env.immich.thumb_calls == []
    assert env.get(scan_id) is None
    with Session(env.engine) as sess:
        assert sess.exec(select(DBPhotoUpgradeAsset)).all() == []
        assert sess.exec(select(DBPhotoUpgradeSuggestion)).all() == []


def test_a_scan_failed_meanwhile_writes_nothing_more(env, broker):
    env.memory([88])
    env.immich.add("a88", _album_thumb(88))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.run_one()   # prepare
    broker.run_one()   # fingerprint
    scan_job.mark_scan_interrupted(scan_id, "stopped")
    broker.drain()     # match

    scan = env.get(scan_id)
    assert (scan.status, scan.error_message) == ("failed", "stopped")
    assert env.suggestions(scan_id) == []


def test_a_job_that_cannot_be_queued_fails_the_scan(env, broker, monkeypatch):
    monkeypatch.setattr(scan_job, "enqueue", lambda *a, **k: False)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)

    assert env.get(scan_id).status == "failed"
    assert env.get(scan_id).error_message == scan_job.REASON_UNAVAILABLE


def test_an_unexpected_error_fails_the_scan_with_a_neutral_sentence(env, broker, monkeypatch):
    env.memory([89])

    def boom(*args, **kwargs):
        raise RuntimeError("https://user:secret@immich.example")

    monkeypatch.setattr(scan_job, "match", boom)
    scan_id = env.scan()

    scan_job.start_scan(scan_id)
    broker.drain()

    assert env.get(scan_id).error_message == scan_job.REASON_FAILED


def test_a_scan_no_longer_pending_is_not_started(env, broker):
    scan_id = env.scan(status="failed")

    scan_job.start_scan(scan_id)
    broker.drain()

    assert broker.ran == ["run_scan_prepare"]
    assert env.get(scan_id).status == "failed"
    assert env.immich.windows == []


# ── Interruptions ─────────────────────────────────────────────────────────────

class _FakeRQJob:
    def __init__(self, args):
        self.args = args
        self.id = "rq-1"


@pytest.mark.parametrize("job", ["run_scan_prepare", "run_scan_fingerprint", "run_scan_match"])
def test_a_killed_work_horse_fails_its_scan(env, job):
    scan_id = env.scan(status="scanning")

    worker_mod._work_horse_killed_handler(
        _FakeRQJob((getattr(scan_job, job), scan_id)), 123, 9, None)

    scan = env.get(scan_id)
    assert scan.status == "failed"
    assert scan.error_message == scan_job.REASON_INTERRUPTED


def test_the_interrupt_handler_leaves_a_finished_scan_alone(env):
    scan_id = env.scan(status="ready")
    scan_job.mark_scan_interrupted(scan_id, scan_job.REASON_INTERRUPTED)
    assert env.get(scan_id).status == "ready"


def test_the_startup_sweep_fails_scanning_scans_only(env):
    scanning = env.scan(status="scanning")
    pending = env.scan(env.mate, status="pending", include_memories=False)
    ready = env.scan(status="ready", created_at=time.time() - 60)

    assert scan_job.sweep_scanning_scans() == 1

    assert env.get(scanning).status == "failed"
    assert env.get(scanning).error_message == scan_job.REASON_INTERRUPTED
    assert env.get(pending).status == "pending"
    assert env.get(ready).status == "ready"


class _FakeWorker:
    order: list = []

    def __init__(self, queues, connection=None, work_horse_killed_handler=None):
        self.queues = queues

    def work(self, with_scheduler):
        _FakeWorker.order.append("work")


@pytest.mark.parametrize("queues, swept", [(["poster", "photos", "default", "resolve"], True),
                                           (["default", "resolve"], False)])
def test_the_worker_sweeps_scans_only_when_it_consumes_photos(monkeypatch, queues, swept):
    _FakeWorker.order = []
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/0")
    monkeypatch.setattr(worker_mod, "_connect_with_retry", lambda: object())
    monkeypatch.setattr("rq.Worker", _FakeWorker)
    monkeypatch.setattr(scan_job, "sweep_scanning_scans",
                        lambda: _FakeWorker.order.append("sweep"))

    assert worker_mod.main(queues) == 0

    assert _FakeWorker.order == (["sweep", "work"] if swept else ["work"])


# ── Without a broker ──────────────────────────────────────────────────────────

def test_without_a_broker_the_chain_runs_in_one_loop(env):
    env.memory([90, 91])
    for i in range(260):
        env.immich.add(f"n{i:03d}", _album_thumb(1000 + i))
    env.immich.add("a90", _album_thumb(90))
    scan_id = env.scan()

    scan_job.start_scan(scan_id)

    scan = env.get(scan_id)
    assert scan.status == "ready", scan.error_message
    assert all(r.fingerprinted for r in env.assets(scan_id))
    assert [r.asset_id for r in env.suggestions(scan_id)] == ["a90", None]


# ── Rejections lifted during the match (U9-R3-1) ──────────────────────────────

def _during_match(monkeypatch, act):
    """Run *act* after the match read the rejections, before the earlier
    scans are reconciled and deleted."""
    real_match = scan_job.match

    def match_then_act(*args, **kwargs):
        result = real_match(*args, **kwargs)
        act()
        return result

    monkeypatch.setattr(scan_job, "match", match_then_act)
    return real_match


def test_an_un_rejection_during_the_match_is_not_carried(env, broker, monkeypatch):
    mid, (uuid,) = env.memory([130])
    env.immich.add("frame-a", _album_thumb(130))
    key = scan_job.photo_key("memory", mid, uuid)
    first = _scan_once(env, broker)
    _reject_in_review(env, first, uuid)
    scan_job.record_rejection(first, key, "frame-a")

    def un_reject():
        _set_row(env, first, uuid, state="proposed", selected=True)
        scan_job.remove_rejection(first, key, "frame-a")

    real_match = _during_match(monkeypatch, un_reject)
    second = _scan_once(env, broker)

    assert env.get(first) is None
    assert (key, "frame-a") not in _pairs(env, second)
    (row,) = env.suggestions(second)
    # What the match gave without the rejection's made-up row.
    assert (row.state, row.asset_id, row.selected) == ("no_match", None, False)

    monkeypatch.setattr(scan_job, "match", real_match)
    third = _scan_once(env, broker)
    (row,) = env.suggestions(third)
    assert (row.state, row.asset_id) == ("proposed", "frame-a")


def test_choosing_a_frame_back_during_the_match_is_not_carried(env, broker, monkeypatch):
    mid, (uuid,) = env.memory([131])
    env.immich.add("frame-a", _album_thumb(131))
    env.immich.add("frame-b", _album_thumb(131))
    key = scan_job.photo_key("memory", mid, uuid)
    first = _scan_once(env, broker)
    rejected = _reject_in_review(env, first, uuid)
    scan_job.record_rejection(first, key, rejected)

    # "Choose another" picks the rejected frame back; only the row changes.
    real_match = _during_match(monkeypatch, lambda: _set_row(
        env, first, uuid, state="proposed", asset_id=rejected, selected=True))
    second = _scan_once(env, broker)

    assert env.get(first) is None
    assert (key, rejected) not in _pairs(env, second)
    (row,) = env.suggestions(second)
    assert not (row.state == "rejected" and row.asset_id == rejected)

    monkeypatch.setattr(scan_job, "match", real_match)
    third = _scan_once(env, broker)
    (row,) = env.suggestions(third)
    offered = {row.asset_id} | {a["asset_id"] for a in json.loads(row.alternatives_json)}
    assert rejected in offered and row.state == "proposed"


def test_a_late_rejection_and_a_late_lift_on_different_photos(env, broker, monkeypatch):
    mid, (p1, p2) = env.memory([132, 133])
    env.immich.add("a132", _album_thumb(132))
    env.immich.add("a133", _album_thumb(133))
    k1, k2 = (scan_job.photo_key("memory", mid, u) for u in (p1, p2))
    first = _scan_once(env, broker)
    _reject_in_review(env, first, p2)
    scan_job.record_rejection(first, k2, "a133")

    def reject_one_lift_the_other():
        _reject_in_review(env, first, p1)
        scan_job.record_rejection(first, k1, "a132")
        _set_row(env, first, p2, state="proposed", selected=True)
        scan_job.remove_rejection(first, k2, "a133")

    real_match = _during_match(monkeypatch, reject_one_lift_the_other)
    second = _scan_once(env, broker)

    assert _pairs(env, second) == {(k1, "a132")}
    one, two = env.suggestions(second)
    assert (one.state, one.asset_id, one.selected) == ("rejected", "a132", False)
    assert (two.state, two.asset_id, two.selected) == ("no_match", None, False)

    monkeypatch.setattr(scan_job, "match", real_match)
    third = _scan_once(env, broker)
    one, two = env.suggestions(third)
    assert (one.state, one.asset_id) == ("rejected", "a132")
    assert (two.state, two.asset_id) == ("proposed", "a133")


def test_a_failed_delete_of_earlier_scans_rolls_the_match_back(env, broker, monkeypatch):
    """Everything the match writes shares one transaction with the delete of
    the earlier scans: when the delete fails, none of it stays."""
    mid, (uuid,) = env.memory([140])
    env.immich.add("frame-a", _album_thumb(140))
    env.immich.add("frame-b", _album_thumb(140))
    key = scan_job.photo_key("memory", mid, uuid)
    first = _scan_once(env, broker)
    rejected = _reject_in_review(env, first, uuid)
    scan_job.record_rejection(first, key, rejected)
    before_first = (env.get(first).status, _pairs(env, first),
                    [(r.state, r.asset_id) for r in env.suggestions(first)])

    def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    real_delete = scan_job.delete_photo_upgrade_scans
    monkeypatch.setattr(scan_job, "delete_photo_upgrade_scans", locked)
    second = env.scan()
    pairs_before = env.get(second).rejected_pairs_json
    scan_job.start_scan(second)
    broker.drain()

    scan = env.get(second)
    assert (scan.status, scan.error_message) == ("failed", scan_job.REASON_FAILED)
    assert env.suggestions(second) == []
    assert scan.rejected_pairs_json == pairs_before
    assert (env.get(first).status, _pairs(env, first),
            [(r.state, r.asset_id) for r in env.suggestions(first)]) == before_first
    assert (key, rejected) in _pairs(env, first)

    monkeypatch.setattr(scan_job, "delete_photo_upgrade_scans", real_delete)
    third = _scan_once(env, broker)
    assert env.get(first) is None and env.get(second) is None
    (row,) = env.suggestions(third)
    assert row.state == "proposed" and row.asset_id != rejected
    assert (key, rejected) in _pairs(env, third)
