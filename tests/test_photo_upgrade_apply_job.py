"""The apply job chain of the Immich photo upgrade (issue #33, U11 of
docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

Immich is a fake client; trip photos are real files in a temporary data
directory, replaced through the real ``api/photo_replace.py``; the database
is a file. Covers:
  * 60 rows in 3 batches, a JPEG taken from Immich's full-size rendition;
  * each error code;
  * the storage-full stop, then a resume finishing the rest;
  * a photo deleted during the apply becoming ``stale``;
  * a write conflict retried in the next batch;
  * recovery from ``applying`` both ways, exact copies included;
  * the killed-work-horse handler and the worker's startup sweep stopping the
    apply as ``interrupted``;
  * a role removed between scan and apply;
  * a HEIC original stored as JPEG;
  * the inline loop without a broker;
  * starts refused, a job that cannot be queued, an unexpected failure, and
    the batch time budget.
"""
from __future__ import annotations

import io
import json
import random
import uuid as uuid_lib
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as mem_mod
import api.photo_replace as photo_replace_mod
import models.db as db_module
import src.jobs.worker as worker_mod
import src.photo_upgrade.apply_job as apply_job
import src.photo_upgrade.scan_job as scan_job
from api.photo_order import dump_state, load_state
from api.photo_write import PhotoWriteConflict
from models.project_db import (
    DBJournalEntry,
    DBMemory,
    DBPhotoUpgradeScan,
    DBPhotoUpgradeSuggestion,
    DBProject,
    DBProjectMember,
)
from models.billing import UserUsage
from models.user import ImmichToken, UserInfo
from src.exceptions.errors import QuotaExceeded
from src.immich.client import ImmichRejected, ImmichUnreachable
from src.utils import photo_store  # noqa: F401 — registers the HEIF opener

_DAY = "2026-05-02"


# ── Images ────────────────────────────────────────────────────────────────────

def _pattern(seed: int, size=(400, 300)) -> Image.Image:
    rnd = random.Random(seed)
    small = Image.new("RGB", (8, 6))
    small.putdata([tuple(rnd.randrange(256) for _ in range(3)) for _ in range(48)])
    return small.resize(size, Image.NEAREST)


def _encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _original(seed: int) -> bytes:
    """What Immich serves for the photo: larger than the stored thumbnail."""
    return _encode(_pattern(seed, (800, 600)), "JPEG")


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Resp:
    """A streamed Immich answer: *body* in chunks, then *error* if any."""

    def __init__(self, body: bytes, error: Exception | None = None, chunk: int = 4096):
        self.body, self.error, self.chunk = body, error, chunk
        self.closed = False

    def iter_content(self, chunk_size=None):
        for i in range(0, len(self.body), self.chunk):
            yield self.body[i:i + self.chunk]
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


class _FakeImmich:
    def __init__(self):
        self.bodies: dict = {}
        self.hooks: dict = {}
        self.calls: list = []
        self.responses: list = []

    def _open(self, kind, asset_id):
        self.calls.append((kind, asset_id))
        hook = self.hooks.get(asset_id)
        if hook is not None:
            hook()
        value = self.bodies[asset_id]
        if isinstance(value, Exception):
            raise value
        resp = value if isinstance(value, _Resp) else _Resp(value)
        self.responses.append(resp)
        return resp

    def open_fullsize(self, asset_id):
        return self._open("fullsize", asset_id)

    def open_original(self, asset_id):
        return self._open("original", asset_id)


class _Broker:
    def __init__(self):
        self.jobs: list = []
        self.ran: list = []

    def enqueue(self, queue_name, func, *args, **kwargs):
        assert queue_name == "photos"
        assert kwargs.get("allow_inline") is False
        self.jobs.append((func, args))
        return True

    def run_one(self) -> None:
        func, args = self.jobs.pop(0)
        self.ran.append(func.__name__)
        func(*args)

    def drain(self) -> list:
        while self.jobs:
            assert len(self.ran) < 50, "the chain never ends"
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

    # Trip photos

    def memory(self, n: int, seed: int = 0) -> tuple:
        uuids = [str(uuid_lib.uuid4()) for _ in range(n)]
        with Session(self.engine) as sess:
            row = DBMemory(project_id=self.project, date=_DAY, photos_json=json.dumps(uuids),
                           photo_order_json=dump_state(load_state(None)))
            sess.add(row)
            sess.commit()
            mid = row.id
        self._write(self.folder("memory", mid), uuids, seed)
        return mid, uuids

    def journal(self, n: int, author: int, seed: int = 0) -> tuple:
        uuids = [str(uuid_lib.uuid4()) for _ in range(n)]
        with Session(self.engine) as sess:
            row = DBJournalEntry(project_id=self.project, user_info_id=author, date=_DAY,
                                 photos_json=json.dumps(uuids))
            sess.add(row)
            sess.commit()
            jid = row.id
        self._write(self.folder("journal", jid, author), uuids, seed)
        return jid, uuids

    def folder(self, kind: str, artifact_id: int, uid: int | None = None) -> Path:
        name = "memories" if kind == "memory" else "journal"
        return self.tmp / "users" / str(uid or self.owner) / name / str(artifact_id)

    def _write(self, folder: Path, uuids, seed: int) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(uuids):
            img = _pattern(seed + i, (200, 150))
            (folder / f"{name}.jpg").write_bytes(_encode(img, "JPEG"))
            (folder / f"{name}_thumb.jpg").write_bytes(_encode(img, "JPEG"))

    def photos(self, kind: str, artifact_id: int) -> list:
        model = DBMemory if kind == "memory" else DBJournalEntry
        with Session(self.engine) as sess:
            return json.loads(sess.get(model, artifact_id).photos_json)

    def sources(self, kind: str, artifact_id: int) -> dict:
        model = DBMemory if kind == "memory" else DBJournalEntry
        with Session(self.engine) as sess:
            return load_state(sess.get(model, artifact_id).photo_order_json)["sources"]

    def set_photos(self, kind: str, artifact_id: int, photos: list, sources=None) -> None:
        model = DBMemory if kind == "memory" else DBJournalEntry
        with Session(self.engine) as sess:
            row = sess.get(model, artifact_id)
            state = load_state(row.photo_order_json)
            if sources is not None:
                state["sources"] = sources
            row.photos_json = json.dumps(photos)
            row.photo_order_json = dump_state(state)
            sess.add(row)
            sess.commit()

    # Scans and rows

    def scan(self, user=None, *, status="ready", apply_status="idle",
             include_memories=True) -> int:
        with Session(self.engine) as sess:
            row = DBPhotoUpgradeScan(project_id=self.project, user_info_id=user or self.owner,
                                     album_id="album", status=status,
                                     include_memories=include_memories,
                                     apply_status=apply_status)
            sess.add(row)
            sess.commit()
            return row.id

    def row(self, scan_id, kind, artifact_id, photo_uuid, position, asset_id, *,
            mime="image/jpeg", body: bytes | None = None, asset_bytes=None,
            state="proposed", selected=True) -> int:
        if body is not None:
            self.immich.bodies[asset_id] = body
        with Session(self.engine) as sess:
            row = DBPhotoUpgradeSuggestion(
                scan_id=scan_id, artifact_kind=kind, artifact_id=artifact_id,
                photo_uuid=photo_uuid, position=position, date=_DAY, state=state,
                confidence="high", selected=selected, asset_id=asset_id, score=0,
                asset_mime=mime, asset_bytes=asset_bytes)
            sess.add(row)
            sess.commit()
            return row.id

    def get(self, scan_id) -> DBPhotoUpgradeScan:
        with Session(self.engine) as sess:
            return sess.get(DBPhotoUpgradeScan, scan_id)

    def sugg(self, row_id) -> DBPhotoUpgradeSuggestion:
        with Session(self.engine) as sess:
            return sess.get(DBPhotoUpgradeSuggestion, row_id)

    def states(self, scan_id) -> list:
        with Session(self.engine) as sess:
            return [r.state for r in sess.exec(
                select(DBPhotoUpgradeSuggestion)
                .where(DBPhotoUpgradeSuggestion.scan_id == scan_id)
                .order_by(DBPhotoUpgradeSuggestion.id)).all()]


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'apply.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    immich = _FakeImmich()
    keys = []

    def make_client(server_url, api_key):
        keys.append((server_url, api_key))
        return immich

    monkeypatch.setattr(apply_job, "_make_client", make_client)
    monkeypatch.setattr(apply_job, "queue_available", lambda: False)
    e = _Env(engine, tmp_path, immich)
    e.keys = keys
    yield e
    engine.dispose()


@pytest.fixture
def broker(monkeypatch):
    b = _Broker()
    monkeypatch.setattr(apply_job, "queue_available", lambda: True)
    monkeypatch.setattr(apply_job, "enqueue", b.enqueue)
    return b


def _one_row(env, **kw) -> tuple:
    """A ready scan with one selected row for a one-photo memory."""
    mid, uuids = env.memory(1)
    scan_id = env.scan()
    kw.setdefault("body", _original(500))
    row_id = env.row(scan_id, "memory", mid, uuids[0], 0, "asset-1", **kw)
    return scan_id, row_id, mid, uuids[0]


# ── The chain ─────────────────────────────────────────────────────────────────

def test_60_rows_take_three_batches_and_replace_in_place(env, broker):
    mid, uuids = env.memory(60)
    scan_id = env.scan()
    rows = [env.row(scan_id, "memory", mid, u, i, f"asset-{i}", body=_original(100 + i))
            for i, u in enumerate(uuids)]

    apply_job.start_apply(scan_id)
    assert env.get(scan_id).apply_status == "pending"
    assert broker.drain() == ["run_apply_batch"] * 3

    scan = env.get(scan_id)
    assert scan.apply_status == "done"
    assert scan.apply_stopped_reason is None
    applied = [env.sugg(r) for r in rows]
    assert [r.state for r in applied] == ["applied"] * 60
    # Same positions, new photos, each sourced from its asset.
    assert env.photos("memory", mid) == [r.applied_uuid for r in applied]
    assert env.sources("memory", mid) == {r.applied_uuid: f"immich:{r.asset_id}"
                                          for r in applied}
    # D5: the old files are gone, the new ones are Immich's bytes.
    folder = env.folder("memory", mid)
    assert not any((folder / f"{u}.jpg").exists() for u in uuids)
    assert (folder / f"{applied[7].applied_uuid}.jpg").read_bytes() == _original(107)
    # OD3: a JPEG is taken from the full-size rendition, never the original.
    assert {kind for kind, _ in env.immich.calls} == {"fullsize"}
    assert all(r.closed for r in env.immich.responses)
    assert env.keys == [("https://immich.example", "secret-key")] * 3


def test_between_batches_the_apply_waits_as_pending(env, broker):
    mid, uuids = env.memory(30)
    scan_id = env.scan()
    for i, u in enumerate(uuids):
        env.row(scan_id, "memory", mid, u, i, f"asset-{i}", body=_original(i))
    apply_job.start_apply(scan_id)

    broker.run_one()

    assert env.get(scan_id).apply_status == "pending"
    assert env.states(scan_id).count("applied") == 25
    # A restarted worker leaves it to its queued job.
    assert apply_job.sweep_running_applies() == 0
    broker.drain()
    assert env.get(scan_id).apply_status == "done"


def test_unselected_and_other_rows_are_left_alone(env, broker):
    mid, uuids = env.memory(3)
    scan_id = env.scan()
    picked = env.row(scan_id, "memory", mid, uuids[0], 0, "a0", body=_original(1))
    unticked = env.row(scan_id, "memory", mid, uuids[1], 1, "a1", body=_original(2),
                       selected=False)
    rejected = env.row(scan_id, "memory", mid, uuids[2], 2, "a2", body=_original(3),
                       state="rejected")

    apply_job.start_apply(scan_id)
    broker.drain()

    assert env.sugg(picked).state == "applied"
    assert env.sugg(unticked).state == "proposed"
    assert env.sugg(rejected).state == "rejected"
    assert env.immich.calls == [("fullsize", "a0")]
    assert env.get(scan_id).apply_status == "done"


# ── Error codes ───────────────────────────────────────────────────────────────

def _big_header_png() -> bytes:
    """A PNG declaring more than MAX_PHOTO_PIXELS, a few KiB on the wire."""
    return _encode(Image.new("1", (10_001, 10_001)), "PNG")


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize("case, code, fetched", [
    ("raw", "unsupported_format", False),
    ("unknown_type", "unsupported_format", False),
    ("too_large_reported", "too_large", False),
    ("too_large_streamed", "too_large", True),
    ("too_many_pixels", "too_large", True),
    ("not_found", "not_found_in_immich", True),
    ("not_found_400", "not_found_in_immich", True),
    ("not_found_410", "not_found_in_immich", True),
    ("no_permission_original", "immich_permission", True),
    ("no_permission_fullsize", "immich_permission", True),
    ("key_refused_original", "immich_permission", True),
    ("key_refused_fullsize", "immich_permission", True),
    ("unreachable", "immich_unreachable", True),
    ("server_error", "immich_unreachable", True),
    ("broken_stream", "immich_unreachable", True),
    ("not_connected", "immich_unreachable", False),
    ("invalid_image", "invalid_image", True),
])
def test_each_error_code(env, broker, monkeypatch, case, code, fetched):
    kw = {}
    if case == "raw":
        kw = {"mime": "image/x-adobe-dng"}
    elif case == "unknown_type":
        kw = {"mime": None}
    elif case == "too_large_reported":
        kw = {"asset_bytes": 25 * 1024 * 1024 + 1}
    elif case == "too_large_streamed":
        monkeypatch.setattr(apply_job, "MAX_PHOTO_BYTES", 1000)
        kw = {"asset_bytes": 900, "body": b"\xff" * 1001}
    elif case == "too_many_pixels":
        kw = {"mime": "image/png", "body": _big_header_png()}
    elif case == "not_found":
        kw = {"body": ImmichRejected(404)}
    elif case == "not_found_400":
        kw = {"body": ImmichRejected(400)}
    elif case == "not_found_410":
        kw = {"body": ImmichRejected(410)}
    elif case == "no_permission_original":
        # A key without asset.download (OD5): not a missing photo.
        kw = {"mime": "image/heic", "body": ImmichRejected(403)}
    elif case == "no_permission_fullsize":
        kw = {"body": ImmichRejected(403)}
    elif case == "key_refused_original":
        # A revoked or invalid key, as the scan reads it.
        kw = {"mime": "image/heic", "body": ImmichRejected(401)}
    elif case == "key_refused_fullsize":
        kw = {"body": ImmichRejected(401)}
    elif case == "unreachable":
        kw = {"body": ImmichUnreachable("down")}
    elif case == "server_error":
        kw = {"body": ImmichRejected(502)}
    elif case == "broken_stream":
        kw = {"body": _Resp(_original(5)[:100], error=OSError("reset"))}
    elif case == "invalid_image":
        kw = {"body": b"not an image at all"}
    scan_id, row_id, mid, old = _one_row(env, **kw)
    if case == "not_connected":
        with Session(env.engine) as sess:
            sess.delete(sess.exec(select(ImmichToken).where(
                ImmichToken.user_info_id == env.owner)).one())
            sess.commit()

    apply_job.start_apply(scan_id)
    broker.drain()

    row = env.sugg(row_id)
    assert (row.state, row.error_code) == ("failed", code)
    assert bool(env.immich.calls) is fetched
    assert env.photos("memory", mid) == [old]
    assert sorted(p.name for p in env.folder("memory", mid).iterdir()) == sorted(
        [f"{old}.jpg", f"{old}_thumb.jpg"])
    assert env.get(scan_id).apply_status == "done"


def test_a_photo_deleted_before_its_turn_is_stale(env, broker):
    scan_id, row_id, mid, old = _one_row(env)
    env.set_photos("memory", mid, [])

    apply_job.start_apply(scan_id)
    broker.drain()

    row = env.sugg(row_id)
    assert (row.state, row.error_code) == ("stale", "photo_gone")
    assert env.immich.calls == []


def test_a_photo_deleted_during_the_apply_is_stale(env, broker):
    scan_id, row_id, mid, old = _one_row(env)
    # Deleted while its original downloads.
    env.immich.hooks["asset-1"] = lambda: env.set_photos("memory", mid, [])

    apply_job.start_apply(scan_id)
    broker.drain()

    row = env.sugg(row_id)
    assert (row.state, row.error_code) == ("stale", "photo_gone")
    assert env.photos("memory", mid) == []
    # The replacement's files went with it (U8).
    assert {p.name for p in env.folder("memory", mid).iterdir()} == {
        f"{old}.jpg", f"{old}_thumb.jpg"}
    assert env.get(scan_id).apply_status == "done"


# ── Storage full ──────────────────────────────────────────────────────────────

def test_storage_full_stops_and_a_resume_finishes_the_rest(env, broker, monkeypatch):
    mid, uuids = env.memory(3)
    scan_id = env.scan()
    rows = [env.row(scan_id, "memory", mid, u, i, f"asset-{i}", body=_original(10 + i))
            for i, u in enumerate(uuids)]
    full = {"on": False}
    real_check = photo_replace_mod.ensure_storage_quota

    def check(sess, user_id, incoming):
        if full["on"]:
            raise QuotaExceeded("full", plan="free", limit=1, used=1, resource="storage")
        return real_check(sess, user_id, incoming)

    monkeypatch.setattr(photo_replace_mod, "ensure_storage_quota", check)
    # The account fills up as the second photo arrives.
    env.immich.hooks["asset-1"] = lambda: full.update(on=True)

    apply_job.start_apply(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert (scan.apply_status, scan.apply_stopped_reason) == ("stopped", "storage_full")
    assert broker.ran == ["run_apply_batch"]
    first, second, third = (env.sugg(r) for r in rows)
    assert first.state == "applied"
    assert (second.state, second.error_code) == ("proposed", "storage_full")
    assert (third.state, third.error_code) == ("proposed", None)
    assert ("fullsize", "asset-2") not in env.immich.calls
    assert env.photos("memory", mid) == [first.applied_uuid, uuids[1], uuids[2]]

    # Space freed: the resume takes the rows still proposed.
    full["on"] = False
    env.immich.hooks.clear()
    apply_job.start_apply(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert (scan.apply_status, scan.apply_stopped_reason) == ("done", None)
    assert [(env.sugg(r).state, env.sugg(r).error_code) for r in rows] == [
        ("applied", None)] * 3


# ── Write conflicts ───────────────────────────────────────────────────────────

def test_a_write_conflict_is_retried_in_the_next_batch(env, broker, monkeypatch):
    scan_id, row_id, mid, old = _one_row(env)
    real = photo_replace_mod.mutate_photos
    calls = {"n": 0}

    def flaky(kind, artifact_id, compute, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PhotoWriteConflict(kind, artifact_id, 5)
        return real(kind, artifact_id, compute, **kw)

    monkeypatch.setattr(photo_replace_mod, "mutate_photos", flaky)

    apply_job.start_apply(scan_id)
    broker.run_one()

    assert env.sugg(row_id).state == "proposed"
    assert env.get(scan_id).apply_status == "pending"
    assert env.photos("memory", mid) == [old]

    broker.drain()

    row = env.sugg(row_id)
    assert row.state == "applied"
    assert broker.ran == ["run_apply_batch"] * 2
    assert env.photos("memory", mid) == [row.applied_uuid]
    assert env.get(scan_id).apply_status == "done"


# ── Recovery ──────────────────────────────────────────────────────────────────

def test_recovery_puts_back_a_row_whose_replace_never_happened(env):
    scan_id, row_id, mid, old = _one_row(env, state="applying")

    apply_job.recover_interrupted_apply(scan_id)

    row = env.sugg(row_id)
    assert (row.state, row.applied_uuid) == ("proposed", None)


def test_recovery_marks_applied_a_row_whose_replace_committed(env):
    scan_id, row_id, mid, old = _one_row(env, state="applying")
    # The batch died right after the replace committed.
    new = photo_replace_mod.replace_photo_bytes(
        "memory", mid, old, _original(500), owner_dir=str(env.owner), source="immich:asset-1")

    apply_job.recover_interrupted_apply(scan_id)

    row = env.sugg(row_id)
    assert (row.state, row.applied_uuid) == ("applied", new)


def test_recovery_of_exact_copies_tells_the_two_rows_apart(env):
    mid, uuids = env.memory(2)
    scan_id = env.scan()
    done = env.row(scan_id, "memory", mid, uuids[0], 0, "same", state="applied")
    left = env.row(scan_id, "memory", mid, uuids[1], 1, "same", state="applying")
    copy = str(uuid_lib.uuid4())
    env.set_photos("memory", mid, [copy, uuids[1]], sources={copy: "immich:same"})
    with Session(env.engine) as sess:
        row = sess.get(DBPhotoUpgradeSuggestion, done)
        row.applied_uuid = copy
        sess.add(row)
        sess.commit()

    apply_job.recover_interrupted_apply(scan_id)
    # Its own photo is still listed: the other copy's original is not its.
    assert env.sugg(left).state == "proposed"

    # Now its replace committed too.
    second = str(uuid_lib.uuid4())
    env.set_photos("memory", mid, [copy, second],
                   sources={copy: "immich:same", second: "immich:same"})
    with Session(env.engine) as sess:
        row = sess.get(DBPhotoUpgradeSuggestion, left)
        row.state = "applying"
        sess.add(row)
        sess.commit()

    apply_job.recover_interrupted_apply(scan_id)
    row = env.sugg(left)
    assert (row.state, row.applied_uuid) == ("applied", second)


def _tree_bytes(env, uid) -> int:
    return sum(p.stat().st_size for p in (env.tmp / "users" / str(uid)).rglob("*")
               if p.is_file())


def _counted(env, uid) -> int:
    with Session(env.engine) as sess:
        return sess.exec(select(UserUsage).where(UserUsage.user_info_id == uid)).one().storage_bytes


def _seed_usage(env, uid) -> None:
    """The counter as the uploads that wrote the trip's files left it."""
    with Session(env.engine) as sess:
        sess.add(UserUsage(user_info_id=uid, storage_bytes=_tree_bytes(env, uid)))
        sess.commit()


class _Killed(Exception):
    """The batch dying between the list write and the old files' delete
    (RQ's timeout exception lands there, U11-R1-1)."""


def test_a_batch_killed_before_the_old_files_go_is_recovered_with_them(
        env, broker, monkeypatch):
    scan_id, row_id, mid, old = _one_row(env)
    _seed_usage(env, env.owner)
    old_bytes = sum(p.stat().st_size for p in env.folder("memory", mid).iterdir())
    real_delete = photo_replace_mod._delete_files
    deletes = []

    def dies_at_the_delete(kind, folder, owner_dir, uuids):
        if uuids == [old] and not deletes:
            deletes.append(uuids)
            raise _Killed()
        return real_delete(kind, folder, owner_dir, uuids)

    monkeypatch.setattr(photo_replace_mod, "_delete_files", dies_at_the_delete)

    apply_job.start_apply(scan_id)
    broker.drain()

    row = env.sugg(row_id)
    assert (row.state, row.applied_uuid) == ("applied", env.photos("memory", mid)[0])
    assert env.get(scan_id).apply_stopped_reason == "interrupted"
    # The old photo's files went, as the replace would have deleted them.
    assert {p.name for p in env.folder("memory", mid).iterdir()} == {
        f"{row.applied_uuid}.jpg", f"{row.applied_uuid}_thumb.jpg"}
    # And the counter is what a clean replace leaves: the files on disk.
    new_bytes = _tree_bytes(env, env.owner)
    assert _counted(env, env.owner) == new_bytes
    assert new_bytes != old_bytes


def test_a_killed_journal_batch_is_recovered_in_the_runners_tree(env, broker, monkeypatch):
    # A companion's own entry: its files are in their tree, not the owner's.
    jid, uuids = env.journal(1, env.mate)
    old = uuids[0]
    env.memory(1, seed=40)   # the owner's tree and counter, to stay as they are
    scan_id = env.scan(env.mate, include_memories=False)
    row_id = env.row(scan_id, "journal", jid, old, 0, "asset-1", body=_original(500))
    _seed_usage(env, env.mate)
    _seed_usage(env, env.owner)
    owner_tree = sorted(p.relative_to(env.tmp) for p in
                        (env.tmp / "users" / str(env.owner)).rglob("*"))
    owner_counted = _counted(env, env.owner)
    real_delete = photo_replace_mod._delete_files
    deletes = []

    def dies_at_the_delete(kind, folder, owner_dir, uuids):
        if uuids == [old] and not deletes:
            deletes.append(uuids)
            raise _Killed()
        return real_delete(kind, folder, owner_dir, uuids)

    monkeypatch.setattr(photo_replace_mod, "_delete_files", dies_at_the_delete)

    apply_job.start_apply(scan_id)
    broker.drain()

    row = env.sugg(row_id)
    assert (row.state, row.applied_uuid) == ("applied", env.photos("journal", jid)[0])
    assert env.get(scan_id).apply_stopped_reason == "interrupted"
    assert {p.name for p in env.folder("journal", jid, env.mate).iterdir()} == {
        f"{row.applied_uuid}.jpg", f"{row.applied_uuid}_thumb.jpg"}
    assert _counted(env, env.mate) == _tree_bytes(env, env.mate)
    assert sorted(p.relative_to(env.tmp) for p in
                  (env.tmp / "users" / str(env.owner)).rglob("*")) == owner_tree
    assert _counted(env, env.owner) == owner_counted


def test_recovery_with_the_old_files_already_gone_uncounts_nothing(env):
    scan_id, row_id, mid, old = _one_row(env, state="applying")
    _seed_usage(env, env.owner)
    # The replace finished, files and counter included; only the row was
    # left applying.
    photo_replace_mod.replace_photo_bytes(
        "memory", mid, old, _original(500), owner_dir=str(env.owner), source="immich:asset-1")
    counted = _counted(env, env.owner)
    assert counted == _tree_bytes(env, env.owner)

    apply_job.recover_interrupted_apply(scan_id)

    assert env.sugg(row_id).state == "applied"
    assert _counted(env, env.owner) == counted


def test_a_proposed_verdict_deletes_nothing(env):
    scan_id, row_id, mid, old = _one_row(env, state="applying")
    _seed_usage(env, env.owner)
    before = sorted(p.name for p in env.folder("memory", mid).iterdir())
    counted = _counted(env, env.owner)

    apply_job.recover_interrupted_apply(scan_id)

    assert env.sugg(row_id).state == "proposed"
    assert sorted(p.name for p in env.folder("memory", mid).iterdir()) == before
    assert _counted(env, env.owner) == counted


def test_recovery_ignores_an_original_of_the_asset_from_an_earlier_apply(env):
    # An exact copy upgraded by an earlier scan's apply holds the asset's
    # source; this row's own photo is still listed, so its replace never ran.
    mid, uuids = env.memory(1)
    earlier = str(uuid_lib.uuid4())
    env.set_photos("memory", mid, [earlier, uuids[0]], sources={earlier: "immich:same"})
    scan_id = env.scan()
    row_id = env.row(scan_id, "memory", mid, uuids[0], 1, "same", state="applying")

    apply_job.recover_interrupted_apply(scan_id)

    row = env.sugg(row_id)
    assert (row.state, row.applied_uuid) == ("proposed", None)


def test_recovery_of_a_photo_gone_another_way_puts_it_back(env, broker):
    mid, uuids = env.memory(1)
    scan_id = env.scan(apply_status="stopped")
    row_id = env.row(scan_id, "memory", mid, uuids[0], 0, "asset-1", state="applying",
                     body=_original(1))
    env.set_photos("memory", mid, [])

    apply_job.recover_interrupted_apply(scan_id)
    assert env.sugg(row_id).state == "proposed"

    # The resume then finds it gone.
    apply_job.start_apply(scan_id)
    broker.drain()
    assert (env.sugg(row_id).state, env.sugg(row_id).error_code) == ("stale", "photo_gone")


# ── Interruptions ─────────────────────────────────────────────────────────────

class _FakeRQJob:
    def __init__(self, args):
        self.args = args
        self.id = "rq-1"


def test_a_killed_work_horse_stops_the_apply_as_interrupted(env):
    mid, uuids = env.memory(2)
    scan_id = env.scan(apply_status="running")
    replaced = env.row(scan_id, "memory", mid, uuids[0], 0, "a0", state="applying")
    untouched = env.row(scan_id, "memory", mid, uuids[1], 1, "a1", state="applying")
    new = photo_replace_mod.replace_photo_bytes(
        "memory", mid, uuids[0], _original(1), owner_dir=str(env.owner), source="immich:a0")

    worker_mod._work_horse_killed_handler(
        _FakeRQJob((apply_job.run_apply_batch, scan_id)), 123, 9, None)

    scan = env.get(scan_id)
    assert (scan.apply_status, scan.apply_stopped_reason) == ("stopped", "interrupted")
    assert (env.sugg(replaced).state, env.sugg(replaced).applied_uuid) == ("applied", new)
    assert env.sugg(untouched).state == "proposed"


def test_an_interrupted_apply_resumes(env, broker):
    scan_id, row_id, mid, old = _one_row(env)
    with Session(env.engine) as sess:
        scan = sess.get(DBPhotoUpgradeScan, scan_id)
        scan.apply_status = "running"
        sess.add(scan)
        sess.commit()
    apply_job.mark_apply_interrupted(scan_id, "interrupted")

    apply_job.start_apply(scan_id)
    broker.drain()

    assert env.sugg(row_id).state == "applied"
    assert (env.get(scan_id).apply_status, env.get(scan_id).apply_stopped_reason) == (
        "done", None)


def test_the_interrupt_handler_leaves_a_finished_apply_alone(env):
    scan_id = env.scan(apply_status="done")
    apply_job.mark_apply_interrupted(scan_id, "interrupted")
    assert env.get(scan_id).apply_status == "done"


def test_the_startup_sweep_stops_running_applies_only(env):
    mid, uuids = env.memory(1)
    running = env.scan(apply_status="running")
    stuck = env.row(running, "memory", mid, uuids[0], 0, "a0", state="applying")
    pending = env.scan(env.mate, apply_status="pending", include_memories=False)
    done = env.scan(env.mate, apply_status="done", include_memories=False)

    assert apply_job.sweep_running_applies() == 1

    assert (env.get(running).apply_status, env.get(running).apply_stopped_reason) == (
        "stopped", "interrupted")
    assert env.sugg(stuck).state == "proposed"
    assert env.get(pending).apply_status == "pending"
    assert env.get(done).apply_status == "done"


class _FakeWorker:
    order: list = []

    def __init__(self, queues, connection=None, work_horse_killed_handler=None):
        self.queues = queues

    def work(self, with_scheduler):
        _FakeWorker.order.append("work")


@pytest.mark.parametrize("queues, swept", [(["poster", "photos", "default", "resolve"], True),
                                           (["default", "resolve"], False)])
def test_the_worker_sweeps_applies_only_when_it_consumes_photos(monkeypatch, queues, swept):
    _FakeWorker.order = []
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/0")
    monkeypatch.setattr(worker_mod, "_connect_with_retry", lambda: object())
    monkeypatch.setattr("rq.Worker", _FakeWorker)
    monkeypatch.setattr(scan_job, "sweep_scanning_scans",
                        lambda: _FakeWorker.order.append("scans"))
    monkeypatch.setattr(apply_job, "sweep_running_applies",
                        lambda: _FakeWorker.order.append("applies"))

    assert worker_mod.main(queues) == 0

    assert _FakeWorker.order == (["scans", "applies", "work"] if swept else ["work"])


# ── Roles ─────────────────────────────────────────────────────────────────────

def test_a_companion_removed_between_scan_and_apply_changes_nothing(env, broker):
    jid, uuids = env.journal(2, env.mate)
    scan_id = env.scan(env.mate, include_memories=False)
    rows = [env.row(scan_id, "journal", jid, u, i, f"a{i}", body=_original(i))
            for i, u in enumerate(uuids)]
    with Session(env.engine) as sess:
        sess.delete(sess.exec(select(DBProjectMember).where(
            DBProjectMember.user_info_id == env.mate)).one())
        sess.commit()

    apply_job.start_apply(scan_id)
    broker.drain()

    assert [env.sugg(r).state for r in rows] == ["stale", "stale"]
    assert env.immich.calls == []
    assert env.photos("journal", jid) == uuids


def test_an_owner_no_longer_owner_cannot_apply_memory_rows(env, broker):
    scan_id, row_id, mid, old = _one_row(env)
    with Session(env.engine) as sess:
        project = sess.get(DBProject, env.project)
        project.user_info_id = env.mate
        sess.add(project)
        sess.commit()

    apply_job.start_apply(scan_id)
    broker.drain()

    assert env.sugg(row_id).state == "stale"
    assert env.photos("memory", mid) == [old]


def test_a_companion_applies_their_own_journal_photos_in_their_tree(env, broker):
    jid, uuids = env.journal(1, env.mate)
    scan_id = env.scan(env.mate, include_memories=False)
    row_id = env.row(scan_id, "journal", jid, uuids[0], 0, "a0", body=_original(1))

    apply_job.start_apply(scan_id)
    broker.drain()

    row = env.sugg(row_id)
    assert row.state == "applied"
    assert (env.folder("journal", jid, env.mate) / f"{row.applied_uuid}.jpg").is_file()
    assert env.sources("journal", jid) == {row.applied_uuid: "immich:a0"}


# ── Formats ───────────────────────────────────────────────────────────────────

def test_a_heic_original_is_stored_as_jpeg(env, broker):
    heic = _encode(_pattern(7, (320, 240)), "HEIF", quality=80)
    scan_id, row_id, mid, old = _one_row(env, mime="image/heic", body=heic)

    apply_job.start_apply(scan_id)
    broker.drain()

    row = env.sugg(row_id)
    assert row.state == "applied", row.error_code
    assert env.immich.calls == [("original", "asset-1")]
    stored = (env.folder("memory", mid) / f"{row.applied_uuid}.jpg").read_bytes()
    with Image.open(io.BytesIO(stored)) as img:
        assert img.format == "JPEG"
        assert img.size == (320, 240)


# ── Without a broker ──────────────────────────────────────────────────────────

def test_without_a_broker_every_batch_runs_in_one_call(env):
    mid, uuids = env.memory(30)
    scan_id = env.scan()
    for i, u in enumerate(uuids):
        env.row(scan_id, "memory", mid, u, i, f"asset-{i}", body=_original(i))

    apply_job.start_apply(scan_id)

    assert env.get(scan_id).apply_status == "done"
    assert env.states(scan_id) == ["applied"] * 30


# ── Starting, queueing, failing ───────────────────────────────────────────────

@pytest.mark.parametrize("status, apply_status", [("ready", "running"), ("scanning", "idle"),
                                                  ("failed", "idle")])
def test_start_apply_refuses_a_running_apply_or_a_scan_not_ready(env, broker, status,
                                                                 apply_status):
    scan_id = env.scan(status=status, apply_status=apply_status)
    apply_job.start_apply(scan_id)
    assert broker.jobs == []
    assert env.get(scan_id).apply_status == apply_status


def test_a_second_job_for_the_same_apply_stops_quietly(env, broker):
    scan_id, row_id, mid, old = _one_row(env)
    apply_job.start_apply(scan_id)
    apply_job.start_apply(scan_id)      # a caller that set "pending" itself

    broker.drain()

    assert env.sugg(row_id).state == "applied"
    assert env.immich.calls == [("fullsize", "asset-1")]
    assert env.get(scan_id).apply_status == "done"


def test_a_job_that_cannot_be_queued_stops_the_apply(env, broker, monkeypatch):
    scan_id, row_id, mid, old = _one_row(env)
    monkeypatch.setattr(apply_job, "enqueue", lambda *a, **k: False)

    apply_job.start_apply(scan_id)

    scan = env.get(scan_id)
    assert (scan.apply_status, scan.apply_stopped_reason) == ("stopped", "interrupted")
    assert env.sugg(row_id).state == "proposed"


def test_an_unexpected_failure_stops_the_apply_and_recovers_the_row(env, broker, monkeypatch):
    scan_id, row_id, mid, old = _one_row(env)

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(apply_job, "replace_photo_bytes", boom)

    apply_job.start_apply(scan_id)
    broker.drain()

    scan = env.get(scan_id)
    assert (scan.apply_status, scan.apply_stopped_reason) == ("stopped", "interrupted")
    assert env.sugg(row_id).state == "proposed"


def test_a_slow_batch_hands_over_to_the_next_job(env, broker, monkeypatch):
    mid, uuids = env.memory(3)
    scan_id = env.scan()
    for i, u in enumerate(uuids):
        env.row(scan_id, "memory", mid, u, i, f"asset-{i}", body=_original(i))
    clock = iter(range(0, 10_000, 400))
    monkeypatch.setattr(apply_job, "_clock", lambda: next(clock))

    apply_job.start_apply(scan_id)
    broker.drain()

    # Each row takes "400 s": one row a batch, never none.
    assert broker.ran == ["run_apply_batch"] * 3
    assert env.states(scan_id) == ["applied"] * 3
    assert env.get(scan_id).apply_status == "done"


def test_a_deleted_scan_stops_the_chain_quietly(env, broker):
    scan_id, row_id, mid, old = _one_row(env)
    apply_job.start_apply(scan_id)
    with Session(env.engine) as sess:
        from models.project_db import delete_photo_upgrade_scans
        delete_photo_upgrade_scans(sess, DBPhotoUpgradeScan.id == scan_id)
        sess.commit()

    broker.drain()

    assert env.immich.calls == []
    assert env.photos("memory", mid) == [old]
