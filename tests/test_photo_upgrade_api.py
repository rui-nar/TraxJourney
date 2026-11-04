"""The photo-upgrade API (issue #33, U10 of docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

Runs against the real app on a file-backed SQLite database, with the job
entry points faked, except where a re-proposed applied row is applied end to
end through the real apply job and a fake Immich client. Covers:
  * the 503 with no worker;
  * an editor refused memories but allowed journal only, a viewer refused, a
    stranger and another user's scan answering 404;
  * ``latest`` with a newer failed scan beside a ready one, and the summary;
  * paging in trip order, and the filters;
  * PATCH: an asset from another day refused, the rejection contract, a row
    changed meanwhile, and an applied row re-proposed and applied again,
    recovery included;
  * select, candidates, resume after ``stopped``, delete;
  * 409 on a scan, apply or delete during an apply, on an apply while a
    newer scan is in progress, and when the scan insert or the apply update
    loses a race;
  * each sweep branch, and its registration in the API's scheduler.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import random
import time
import uuid as uuid_lib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, SQLModel, select

import api.journal as journal_mod
import api.memories as mem_mod
import api.photo_replace as photo_replace_mod
import api.photo_upgrade as pu
import models.db as db_module
import src.photo_upgrade.apply_job as apply_job
import src.photo_upgrade.scan_job as scan_job
from api.deps import get_current_user
from api.photo_order import dump_state, load_state
from api.router import app
from models.billing import UserUsage
from models.project_db import (
    DBJournalEntry,
    DBMemory,
    DBPhotoUpgradeAsset,
    DBPhotoUpgradeScan,
    DBPhotoUpgradeSuggestion,
    DBProject,
    DBProjectMember,
)
from models.user import ImmichToken, UserInfo
from src.auth.account_deletion import delete_user_and_data
from src.billing.entitlements import limits_for_user
from src.photo_upgrade.hashing import fingerprint, to_hex
from src.photo_upgrade.matching import TripPhoto, rank_candidates
from src.photo_upgrade.scan_job import _album_photo, photo_key
from src.project.project_repo import ProjectRepo

_DAY = "2026-05-02"
_BILLING_ENV = ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY",
                "FREE_MAX_PROJECTS", "FREE_MAX_STORAGE_MB")


# ── Images ────────────────────────────────────────────────────────────────────

def _pattern(seed: int, size=(400, 300)) -> Image.Image:
    rnd = random.Random(seed)
    small = Image.new("RGB", (8, 6))
    small.putdata([tuple(rnd.randrange(256) for _ in range(3)) for _ in range(48)])
    return small.resize(size, Image.NEAREST)


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def _original(seed: int) -> bytes:
    return _jpeg(_pattern(seed, (800, 600)))


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def iter_content(self, chunk_size=None):
        yield self.body

    def close(self):
        pass


class _FakeImmich:
    def __init__(self):
        self.bodies: dict = {}

    def open_fullsize(self, asset_id):
        return _Resp(self.bodies[asset_id])

    def open_original(self, asset_id):
        return _Resp(self.bodies[asset_id])


# ── Environment ───────────────────────────────────────────────────────────────

class _Env:
    def __init__(self, engine, tmp_path: Path):
        self.engine = engine
        self.tmp = tmp_path
        with Session(engine) as sess:
            users = [UserInfo(display_name=n, email=f"{n}@example.com")
                     for n in ("owner", "mate", "viewer", "stranger")]
            sess.add_all(users)
            sess.commit()
            self.owner, self.mate, self.viewer, self.stranger = (u.id for u in users)
            project = DBProject(user_info_id=self.owner, name="Trip")
            sess.add(project)
            sess.commit()
            self.project = project.id
            sess.add(DBProjectMember(project_id=self.project, user_info_id=self.mate,
                                     role="editor", invited_by=self.owner))
            sess.add(DBProjectMember(project_id=self.project, user_info_id=self.viewer,
                                     role="viewer", invited_by=self.owner))
            for uid in (self.owner, self.mate):
                sess.add(ImmichToken(user_info_id=uid, server_url="https://immich.example",
                                     api_key="secret-key"))
            sess.commit()
        self.who = self.owner
        self.scans_started: list = []
        self.applies_started: list = []

    def as_(self, uid):
        self.who = uid
        return self

    def url(self, path: str = "") -> str:
        q = "" if self.who == self.owner else f"?owner={self.owner}"
        return f"/api/projects/Trip/photo-upgrade/scans{path}{q}"

    # Trip photos

    def memory(self, n: int, seed: int = 0, date: str = _DAY) -> tuple:
        uuids = [str(uuid_lib.uuid4()) for _ in range(n)]
        with Session(self.engine) as sess:
            row = DBMemory(project_id=self.project, date=date, photos_json=json.dumps(uuids),
                           photo_order_json=dump_state(load_state(None)))
            sess.add(row)
            sess.commit()
            mid = row.id
        folder = self.folder("memory", mid)
        folder.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(uuids):
            raw = _jpeg(_pattern(seed + i, (200, 150)))
            (folder / f"{name}.jpg").write_bytes(raw)
            (folder / f"{name}_thumb.jpg").write_bytes(raw)
        return mid, uuids

    def folder(self, kind: str, artifact_id: int, uid: int | None = None) -> Path:
        name = "memories" if kind == "memory" else "journal"
        return self.tmp / "users" / str(uid or self.owner) / name / str(artifact_id)

    def photos(self, kind: str, artifact_id: int) -> list:
        model = DBMemory if kind == "memory" else DBJournalEntry
        with Session(self.engine) as sess:
            return json.loads(sess.get(model, artifact_id).photos_json)

    def sources(self, kind: str, artifact_id: int) -> dict:
        model = DBMemory if kind == "memory" else DBJournalEntry
        with Session(self.engine) as sess:
            return load_state(sess.get(model, artifact_id).photo_order_json)["sources"]

    # Scans and rows

    def scan(self, user=None, *, status="ready", apply_status="idle", created_at=None,
             include_memories=True, **kw) -> int:
        with Session(self.engine) as sess:
            row = DBPhotoUpgradeScan(project_id=self.project, user_info_id=user or self.owner,
                                     album_id="album", status=status,
                                     include_memories=include_memories,
                                     apply_status=apply_status, **kw)
            if created_at is not None:
                row.created_at = created_at
            sess.add(row)
            sess.commit()
            return row.id

    def asset(self, scan_id, asset_id, *, local_date=_DAY, img: Image.Image | None = None,
              fingerprinted=None, file_bytes=900_000, mime="image/jpeg") -> int:
        fp = fingerprint(_jpeg(img)) if img is not None else None
        with Session(self.engine) as sess:
            row = DBPhotoUpgradeAsset(
                scan_id=scan_id, asset_id=asset_id, local_date=local_date, mime=mime,
                file_bytes=file_bytes, original_width=4000, original_height=3000,
                width=fp.width if fp else 400, height=fp.height if fp else 300,
                ahash=to_hex(fp.ahash) if fp else None, dhash=to_hex(fp.dhash) if fp else None,
                fingerprinted=fp is not None if fingerprinted is None else fingerprinted)
            sess.add(row)
            sess.commit()
            return row.id

    def row(self, scan_id, artifact_id=1, photo_uuid=None, position=0, asset_id="asset-1", *,
            kind="memory", date=_DAY, state="proposed", confidence="high", selected=True,
            asset_bytes=None, current_bytes=None, applied_uuid=None, alternatives=()) -> int:
        with Session(self.engine) as sess:
            row = DBPhotoUpgradeSuggestion(
                scan_id=scan_id, artifact_kind=kind, artifact_id=artifact_id,
                photo_uuid=photo_uuid or str(uuid_lib.uuid4()), position=position, date=date,
                state=state, confidence=confidence, selected=selected, asset_id=asset_id,
                score=0, asset_mime="image/jpeg", asset_bytes=asset_bytes,
                current_bytes=current_bytes, applied_uuid=applied_uuid,
                alternatives_json=json.dumps(list(alternatives)))
            sess.add(row)
            sess.commit()
            return row.id

    def get(self, scan_id) -> DBPhotoUpgradeScan | None:
        with Session(self.engine) as sess:
            return sess.get(DBPhotoUpgradeScan, scan_id)

    def sugg(self, row_id) -> DBPhotoUpgradeSuggestion:
        with Session(self.engine) as sess:
            return sess.get(DBPhotoUpgradeSuggestion, row_id)

    def pairs(self, scan_id) -> list:
        return json.loads(self.get(scan_id).rejected_pairs_json)

    def all_scans(self) -> list:
        with Session(self.engine) as sess:
            return sess.exec(select(DBPhotoUpgradeScan).order_by(DBPhotoUpgradeScan.id)).all()


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = db_module._make_engine(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    db_module._configure_sqlite(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))
    for var in _BILLING_ENV:
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)
    e = _Env(engine, tmp_path)
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(e.who)}
    # No broker: no worker check, and the jobs are faked unless a test says so.
    monkeypatch.setattr(pu, "queue_available", lambda: False)
    monkeypatch.setattr(pu, "start_scan", e.scans_started.append)
    monkeypatch.setattr(pu, "start_apply", e.applies_started.append)
    e.client = TestClient(app)
    try:
        yield e
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _scan_body(**kw) -> dict:
    return {"album_id": "album-1", "album_name": "Italy", "include_memories": False,
            "include_journal": True, **kw}


def _code(resp) -> str:
    return resp.json()["detail"]["code"]


# ── Starting a scan ───────────────────────────────────────────────────────────

def test_no_worker_answers_503_and_writes_nothing(env, monkeypatch):
    ready = env.scan()
    monkeypatch.setattr(pu, "queue_available", lambda: True)
    monkeypatch.setattr(pu, "queue_has_workers", lambda name: name != "photos")

    resp = env.client.post(env.url(), json=_scan_body())
    assert resp.status_code == 503
    assert resp.json()["detail"] == "No worker is processing photo jobs"
    resp = env.client.post(f"/api/photo-upgrade/scans/{ready}/apply")
    assert resp.status_code == 503

    assert [s.id for s in env.all_scans()] == [ready]
    assert env.get(ready).apply_status == "idle"
    assert env.scans_started == [] and env.applies_started == []

    # A worker listening on photos lets both through.
    monkeypatch.setattr(pu, "queue_has_workers", lambda name: name == "photos")
    assert env.client.post(f"/api/photo-upgrade/scans/{ready}/apply").status_code == 202


def test_owner_starts_a_scan_of_memories_and_journal(env):
    resp = env.client.post(env.url(), json=_scan_body(include_memories=True))
    assert resp.status_code == 202
    body = resp.json()
    assert (body["status"], body["album_id"], body["album_name"]) == ("pending", "album-1",
                                                                      "Italy")
    assert body["include_memories"] and body["include_journal"]
    assert env.scans_started == [body["id"]]
    scan = env.get(body["id"])
    assert (scan.user_info_id, scan.project_id) == (env.owner, env.project)


def test_an_editor_is_refused_memories_but_may_scan_journal_only(env):
    env.as_(env.mate)
    resp = env.client.post(env.url(), json=_scan_body(include_memories=True))
    assert resp.status_code == 403
    assert env.all_scans() == []

    resp = env.client.post(env.url(), json=_scan_body())
    assert resp.status_code == 202
    scan = env.get(resp.json()["id"])
    assert (scan.user_info_id, scan.include_memories, scan.include_journal) == (
        env.mate, False, True)
    assert env.scans_started == [scan.id]


def test_a_viewer_is_refused_and_a_stranger_finds_nothing(env):
    env.as_(env.viewer)
    assert env.client.post(env.url(), json=_scan_body()).status_code == 403
    assert env.client.get(env.url("/latest")).status_code == 403
    env.as_(env.stranger)
    assert env.client.post(env.url(), json=_scan_body()).status_code == 404
    assert env.all_scans() == []


def test_a_scan_with_nothing_to_check_is_refused(env):
    resp = env.client.post(env.url(), json=_scan_body(include_journal=False))
    assert resp.status_code == 422
    assert env.all_scans() == []


@pytest.mark.parametrize("status", ["pending", "scanning"])
def test_a_second_scan_while_one_is_active_is_409(env, status):
    env.scan(status=status)
    resp = env.client.post(env.url(), json=_scan_body())
    assert resp.status_code == 409
    assert _code(resp) == "scan_in_progress"
    assert len(env.all_scans()) == 1


def test_a_companions_active_scan_does_not_block_the_owner(env):
    env.scan(env.mate, status="scanning", include_memories=False)
    assert env.client.post(env.url(), json=_scan_body()).status_code == 202


def test_a_scan_insert_refused_by_the_unique_index_is_409_not_500(env, monkeypatch):
    """A second request (a double tap) passed the check at the same time and
    inserted its scan first (R2-3)."""
    real = pu._refuse_if_busy

    def check_then_race(sess, project_id, user_info_id):
        real(sess, project_id, user_info_id)
        env.scan(user_info_id, status="pending")

    monkeypatch.setattr(pu, "_refuse_if_busy", check_then_race)
    resp = env.client.post(env.url(), json=_scan_body())

    assert resp.status_code == 409
    assert _code(resp) == "scan_in_progress"
    assert [s.status for s in env.all_scans()] == ["pending"]
    assert env.scans_started == []


# ── latest and the summary ────────────────────────────────────────────────────

def test_latest_shows_a_newer_failed_scan_beside_the_ready_one(env):
    now = time.time()
    ready = env.scan(created_at=now - 100)
    failed = env.scan(status="failed", created_at=now, error_message="Could not reach it.")

    body = env.client.get(env.url("/latest")).json()
    assert body["scan"]["id"] == ready
    assert body["newer"]["id"] == failed
    assert (body["newer"]["status"], body["newer"]["error_message"]) == (
        "failed", "Could not reach it.")


def test_latest_without_a_newer_scan_or_without_a_ready_one(env):
    assert env.client.get(env.url("/latest")).json()["scan"] is None
    scanning = env.scan(status="scanning", stage="fingerprinting", progress_done=3,
                        progress_total=9)
    body = env.client.get(env.url("/latest")).json()
    assert body["scan"] is None
    assert (body["newer"]["id"], body["newer"]["stage"], body["newer"]["progress_done"],
            body["newer"]["progress_total"]) == (scanning, "fingerprinting", 3, 9)

    with Session(env.engine) as sess:
        row = sess.get(DBPhotoUpgradeScan, scanning)
        row.status = "ready"
        sess.add(row)
        sess.commit()
    body = env.client.get(env.url("/latest")).json()
    assert body["scan"]["id"] == scanning and body["newer"] is None


def test_latest_is_the_callers_own(env):
    env.scan(env.mate, include_memories=False)
    assert env.client.get(env.url("/latest")).json()["scan"] is None


def test_the_summary_numbers(env):
    scan = env.scan()
    # Ticked proposals: +100 and -40, and one with an unknown current size.
    env.row(scan, confidence="high", asset_bytes=600, current_bytes=500)
    env.row(scan, confidence="low", asset_bytes=460, current_bytes=500)
    env.row(scan, confidence="high", asset_bytes=1000, current_bytes=None)
    # Not counted as selected: unticked, or not proposed.
    env.row(scan, confidence="high", selected=False, asset_bytes=9999, current_bytes=1)
    env.row(scan, state="applied", asset_bytes=9999, current_bytes=1)
    env.row(scan, state="no_match", confidence="low", selected=False, asset_id=None)
    env.row(scan, state="rejected", confidence=None, selected=False)
    env.row(scan, state="failed", selected=True)
    with Session(env.engine) as sess:
        sess.add(UserUsage(user_info_id=env.owner, storage_bytes=12345))
        sess.commit()

    summary = env.client.get(env.url("/latest")).json()["scan"]["summary"]
    assert summary["counts"] == {"proposed": 4, "no_match": 1, "already_original": 0,
                                 "rejected": 1, "applying": 0, "applied": 1, "failed": 1,
                                 "stale": 0}
    assert summary["confidence"] == {"high": 3, "low": 1}
    assert summary["selected"] == 3
    assert summary["selected_bytes_delta"] == 100 - 40 + 1000
    assert summary["storage"] == {"used": 12345, "limit": None}


def test_the_summary_gives_the_storage_limit_when_quotas_are_enforced(env, monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "1")
    monkeypatch.setenv("BILLING_ENFORCE_QUOTAS", "1")
    monkeypatch.setenv("FREE_MAX_STORAGE_MB", "7")
    env.scan()
    with Session(env.engine) as sess:
        limit = limits_for_user(sess, env.owner).max_storage_bytes
    assert limit is not None
    summary = env.client.get(env.url("/latest")).json()["scan"]["summary"]
    assert summary["storage"] == {"used": 0, "limit": limit}


def test_latest_counts_the_photos_each_checkbox_covers(env):
    env.memory(3)
    with Session(env.engine) as sess:
        sess.add(DBJournalEntry(project_id=env.project, user_info_id=env.mate, date=_DAY,
                                photos_json=json.dumps(["a", "b"])))
        sess.add(DBJournalEntry(project_id=env.project, user_info_id=None, date=_DAY,
                                photos_json=json.dumps(["c"])))
        sess.commit()
    assert env.client.get(env.url("/latest")).json()["photo_counts"] == {
        "memories": 3, "journal": 1}
    env.as_(env.mate)
    assert env.client.get(env.url("/latest")).json()["photo_counts"] == {
        "memories": None, "journal": 2}


# ── Paging ────────────────────────────────────────────────────────────────────

def test_suggestions_page_in_trip_order(env):
    scan = env.scan()
    keys = [(f"2026-05-{d:02d}", kind, artifact, pos)
            for d in (1, 2, 3) for kind in ("journal", "memory")
            for artifact in (7, 30) for pos in range(20)]
    shuffled = keys[:]
    random.Random(4).shuffle(shuffled)
    for date, kind, artifact, pos in shuffled:
        env.row(scan, artifact, position=pos, kind=kind, date=date)

    seen, cursor, pages = [], None, 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        body = env.client.get(f"/api/photo-upgrade/scans/{scan}/suggestions",
                              params=params).json()
        pages += 1
        seen += [(i["date"], i["artifact_kind"], i["artifact_id"], i["position"])
                 for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(keys) == 240 and pages == 3
    assert seen == sorted(keys)


def test_suggestions_filter_by_state_and_confidence(env):
    scan = env.scan()
    high = env.row(scan, position=0, confidence="high")
    low = env.row(scan, position=1, confidence="low")
    unknown = env.row(scan, position=2, confidence=None)
    none = env.row(scan, position=3, state="no_match", asset_id=None)

    def ids(f):
        resp = env.client.get(f"/api/photo-upgrade/scans/{scan}/suggestions",
                              params={"filter": f})
        return [i["id"] for i in resp.json()["items"]]

    assert ids("confident") == [high]
    assert ids("to_check") == [low, unknown]
    assert ids("no_match") == [none]
    assert ids("") == [high, low, unknown, none]
    assert env.client.get(f"/api/photo-upgrade/scans/{scan}/suggestions",
                          params={"filter": "bogus"}).status_code == 422
    assert env.client.get(f"/api/photo-upgrade/scans/{scan}/suggestions",
                          params={"cursor": "not-a-cursor"}).status_code == 422


# ── Another user's scan ───────────────────────────────────────────────────────

def test_another_users_scan_is_404_on_every_route(env):
    scan = env.scan(env.mate, include_memories=False)
    row = env.row(scan, kind="journal")
    base = f"/api/photo-upgrade/scans/{scan}"
    calls = [
        ("get", f"{base}/suggestions", None),
        ("patch", f"{base}/suggestions/{row}", {"selected": False}),
        ("post", f"{base}/select", {"which": "none"}),
        ("get", f"{base}/candidates?suggestion_id={row}", None),
        ("post", f"{base}/apply", None),
        ("delete", base, None),
    ]
    for method, url, body in calls:
        kw = {"json": body} if body is not None else {}
        resp = getattr(env.client, method)(url, **kw)
        assert resp.status_code == 404, (method, url)
    assert env.get(scan) is not None
    assert env.sugg(row).selected is True
    assert env.applies_started == []


def test_a_companion_removed_from_the_trip_no_longer_reaches_their_scan(env):
    scan = env.scan(env.mate, include_memories=False)
    env.row(scan, kind="journal")
    with Session(env.engine) as sess:
        member = sess.exec(select(DBProjectMember).where(
            DBProjectMember.user_info_id == env.mate)).one()
        sess.delete(member)
        sess.commit()
    env.as_(env.mate)
    assert env.client.get(f"/api/photo-upgrade/scans/{scan}/suggestions").status_code == 404
    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 404
    # They can still discard what they ran.
    assert env.client.delete(f"/api/photo-upgrade/scans/{scan}").status_code == 204
    assert env.get(scan) is None


# ── PATCH ─────────────────────────────────────────────────────────────────────

def _patch(env, scan, row, **body):
    return env.client.patch(f"/api/photo-upgrade/scans/{scan}/suggestions/{row}", json=body)


def test_patch_refuses_an_asset_from_another_day(env):
    scan = env.scan()
    other = env.scan(status="failed")
    row = env.row(scan, asset_id="asset-1")
    env.asset(scan, "far", local_date="2026-05-04")
    env.asset(scan, "next-day", local_date="2026-05-03", file_bytes=1234)
    env.asset(other, "other-scan", local_date=_DAY)

    for asset_id in ("far", "other-scan", "nowhere"):
        resp = _patch(env, scan, row, asset_id=asset_id)
        assert resp.status_code == 422, asset_id
    assert env.sugg(row).asset_id == "asset-1"

    resp = _patch(env, scan, row, asset_id="next-day")
    assert resp.status_code == 200
    got = env.sugg(row)
    assert (got.asset_id, got.asset_bytes, got.state, got.selected, got.confidence) == (
        "next-day", 1234, "proposed", True, "high")
    assert resp.json()["suggestion"]["asset_id"] == "next-day"
    assert resp.json()["summary"]["selected"] == 1


def test_patch_ticks_and_unticks(env):
    scan = env.scan()
    row = env.row(scan, selected=False)
    resp = _patch(env, scan, row, selected=True)
    assert resp.status_code == 200 and env.sugg(row).selected is True
    assert resp.json()["summary"]["selected"] == 1
    assert _patch(env, scan, row, selected=False).status_code == 200
    assert env.sugg(row).selected is False
    no_match = env.row(scan, state="no_match", asset_id=None, selected=False)
    resp = _patch(env, scan, no_match, selected=True)
    assert resp.status_code == 409 and _code(resp) == "not_selectable"


def test_a_reject_records_the_pair_and_an_unreject_lifts_it(env):
    scan = env.scan()
    uuid = str(uuid_lib.uuid4())
    row = env.row(scan, 5, uuid, asset_id="asset-1")
    key = photo_key("memory", 5, uuid)

    assert _patch(env, scan, row, rejected=True).status_code == 200
    got = env.sugg(row)
    assert (got.state, got.selected) == ("rejected", False)
    assert env.pairs(scan) == [[key, "asset-1"]]

    assert _patch(env, scan, row, rejected=False).status_code == 200
    got = env.sugg(row)
    assert (got.state, got.asset_id) == ("proposed", "asset-1")
    assert env.pairs(scan) == []


def test_choosing_an_asset_rejected_for_that_photo_lifts_the_rejection(env):
    scan = env.scan()
    uuid = str(uuid_lib.uuid4())
    row = env.row(scan, 5, uuid, asset_id="asset-1")
    env.asset(scan, "asset-2")
    key = photo_key("memory", 5, uuid)
    assert _patch(env, scan, row, rejected=True).status_code == 200
    # asset-2 was turned down for this photo on an earlier scan, carried here.
    with Session(env.engine) as sess:
        s = sess.get(DBPhotoUpgradeScan, scan)
        s.rejected_pairs_json = json.dumps(sorted([[key, "asset-1"], [key, "asset-2"]]))
        sess.add(s)
        sess.commit()

    assert _patch(env, scan, row, asset_id="asset-2").status_code == 200
    got = env.sugg(row)
    assert (got.state, got.asset_id, got.selected) == ("proposed", "asset-2", True)
    # The pair chosen is lifted; the asset this row turned down stays rejected.
    assert env.pairs(scan) == [[key, "asset-1"]]


def test_patch_on_a_row_being_applied_or_stale_is_409(env):
    scan = env.scan()
    env.asset(scan, "asset-2")
    for state in ("applying", "stale"):
        row = env.row(scan, state=state)
        resp = _patch(env, scan, row, asset_id="asset-2")
        assert resp.status_code == 409 and _code(resp) == "suggestion_locked"
        assert env.sugg(row).state == state


def test_patch_loses_to_a_row_changed_meanwhile(env, monkeypatch):
    """The apply took the row between the read and the write."""
    scan = env.scan()
    row = env.row(scan)
    env.asset(scan, "asset-2")
    real = pu._owned_row

    def read_then_taken(sess, s, suggestion_id):
        got = real(sess, s, suggestion_id)
        with Session(env.engine) as other:
            r = other.get(DBPhotoUpgradeSuggestion, suggestion_id)
            r.state = "applying"
            other.add(r)
            other.commit()
        return got

    monkeypatch.setattr(pu, "_owned_row", read_then_taken)
    resp = _patch(env, scan, row, asset_id="asset-2")
    assert resp.status_code == 409 and _code(resp) == "suggestion_changed"
    got = env.sugg(row)
    assert (got.state, got.asset_id) == ("applying", "asset-1")


def test_patch_on_an_applied_row_re_proposes_the_stored_original(env):
    mid, uuids = env.memory(1)
    applied = uuids[0]   # what the apply stored; the old photo is gone
    scan = env.scan()
    row = env.row(scan, mid, str(uuid_lib.uuid4()), state="applied", applied_uuid=applied,
                  asset_id="asset-1", selected=True)
    env.asset(scan, "asset-2", file_bytes=777)

    resp = _patch(env, scan, row, asset_id="asset-2")
    assert resp.status_code == 200
    got = env.sugg(row)
    assert (got.state, got.photo_uuid, got.applied_uuid, got.asset_id, got.selected) == (
        "proposed", applied, None, "asset-2", True)
    # current_* now describe the original the apply stored.
    assert got.current_bytes == (env.folder("memory", mid) / f"{applied}.jpg").stat().st_size
    assert (got.current_width, got.current_height) == (200, 150)
    assert resp.json()["suggestion"]["current_uuid"] == applied


def test_patch_on_an_applied_row_whose_photo_is_gone_is_409(env):
    mid, _ = env.memory(1)
    scan = env.scan()
    row = env.row(scan, mid, str(uuid_lib.uuid4()), state="applied",
                  applied_uuid=str(uuid_lib.uuid4()))
    env.asset(scan, "asset-2")
    resp = _patch(env, scan, row, asset_id="asset-2")
    assert resp.status_code == 409 and _code(resp) == "photo_gone"
    assert env.sugg(row).state == "applied"


# ── An applied row chosen again, applied end to end (U11-R1-4) ────────────────

@pytest.fixture
def real_apply(env, monkeypatch):
    immich = _FakeImmich()
    monkeypatch.setattr(pu, "start_apply", apply_job.start_apply)
    monkeypatch.setattr(apply_job, "queue_available", lambda: False)
    monkeypatch.setattr(apply_job, "_make_client", lambda url, key: immich)
    immich.bodies.update({"asset-1": _original(501), "asset-2": _original(502)})
    return immich


def _applied_once(env) -> tuple:
    mid, uuids = env.memory(1)
    scan = env.scan()
    env.asset(scan, "asset-1")
    env.asset(scan, "asset-2")
    row = env.row(scan, mid, uuids[0], asset_id="asset-1")
    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 202
    first = env.sugg(row)
    assert first.state == "applied"
    assert env.photos("memory", mid) == [first.applied_uuid]
    assert _patch(env, scan, row, asset_id="asset-2").status_code == 200
    return scan, row, mid, first.applied_uuid


def test_a_re_proposed_applied_row_applies_end_to_end(env, real_apply):
    scan, row, mid, first = _applied_once(env)

    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 202

    got = env.sugg(row)
    assert got.state == "applied", got.error_code
    assert got.photo_uuid == first and got.applied_uuid not in (None, first)
    assert env.photos("memory", mid) == [got.applied_uuid]
    assert env.sources("memory", mid) == {got.applied_uuid: "immich:asset-2"}
    folder = env.folder("memory", mid)
    assert not (folder / f"{first}.jpg").exists()
    assert (folder / f"{got.applied_uuid}.jpg").read_bytes() == _original(502)
    assert env.get(scan).apply_status == "done"


class _Killed(Exception):
    pass


def test_a_re_proposed_applied_row_killed_after_its_replace_is_recovered(
        env, real_apply, monkeypatch):
    scan, row, mid, first = _applied_once(env)
    real_delete = photo_replace_mod._delete_files
    killed = []

    def dies_at_the_delete(kind, folder, owner_dir, uuids):
        if uuids == [first] and not killed:
            killed.append(uuids)
            raise _Killed()
        return real_delete(kind, folder, owner_dir, uuids)

    monkeypatch.setattr(photo_replace_mod, "_delete_files", dies_at_the_delete)
    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 202

    assert killed
    got = env.sugg(row)
    assert env.get(scan).apply_stopped_reason == "interrupted"
    assert (got.state, got.applied_uuid) == ("applied", env.photos("memory", mid)[0])
    assert got.applied_uuid != first
    assert env.sources("memory", mid) == {got.applied_uuid: "immich:asset-2"}
    assert {p.name for p in env.folder("memory", mid).iterdir()} == {
        f"{got.applied_uuid}.jpg", f"{got.applied_uuid}_thumb.jpg"}


def test_a_re_proposed_applied_row_killed_before_its_replace_resumes(
        env, real_apply, monkeypatch):
    scan, row, mid, first = _applied_once(env)
    real_replace = apply_job.replace_photo_bytes

    def dies_first(*a, **kw):
        monkeypatch.setattr(apply_job, "replace_photo_bytes", real_replace)
        raise _Killed()

    monkeypatch.setattr(apply_job, "replace_photo_bytes", dies_first)
    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 202
    got = env.sugg(row)
    assert env.get(scan).apply_stopped_reason == "interrupted"
    assert (got.state, got.photo_uuid) == ("proposed", first)
    assert env.photos("memory", mid) == [first]

    # The resume applies it.
    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 202
    got = env.sugg(row)
    assert got.state == "applied"
    assert env.photos("memory", mid) == [got.applied_uuid]
    assert env.sources("memory", mid) == {got.applied_uuid: "immich:asset-2"}


# ── A failed row ticked again (U10-R1-2) ──────────────────────────────────────

def _failed_row(env, **kw) -> tuple:
    mid, (p,) = env.memory(1)
    scan = env.scan(apply_status="done")
    row = env.row(scan, mid, p, asset_id="asset-1", state="failed", selected=True, **kw)
    with Session(env.engine) as sess:
        r = sess.get(DBPhotoUpgradeSuggestion, row)
        r.error_code = "immich_permission"
        sess.add(r)
        sess.commit()
    return scan, row, mid


def test_ticking_a_failed_row_puts_it_back_and_the_apply_takes_it(env, real_apply):
    scan, row, mid = _failed_row(env, asset_bytes=900, current_bytes=500)

    resp = _patch(env, scan, row, selected=True)

    assert resp.status_code == 200
    got = env.sugg(row)
    assert (got.state, got.selected, got.error_code) == ("proposed", True, None)
    body = resp.json()
    assert (body["suggestion"]["state"], body["suggestion"]["error_code"]) == ("proposed", None)
    summary = body["summary"]
    assert (summary["counts"]["proposed"], summary["counts"]["failed"]) == (1, 0)
    assert (summary["selected"], summary["selected_bytes_delta"]) == (1, 400)

    # The key is fixed now: the next apply takes the row.
    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 202
    got = env.sugg(row)
    assert got.state == "applied"
    assert env.photos("memory", mid) == [got.applied_uuid]
    assert env.sources("memory", mid) == {got.applied_uuid: "immich:asset-1"}


def test_choosing_a_failed_rows_asset_again_puts_it_back(env):
    scan, row, _ = _failed_row(env)
    env.asset(scan, "asset-1")
    assert _patch(env, scan, row, asset_id="asset-1").status_code == 200
    got = env.sugg(row)
    assert (got.state, got.selected, got.error_code) == ("proposed", True, None)


def test_a_failed_row_without_an_asset_still_cannot_be_ticked(env):
    scan = env.scan()
    row = env.row(scan, state="failed", asset_id=None, selected=False)
    resp = _patch(env, scan, row, selected=True)
    assert resp.status_code == 409 and _code(resp) == "not_selectable"
    assert env.sugg(row).state == "failed"


# ── select ────────────────────────────────────────────────────────────────────

def test_select_confident_all_proposed_and_none(env):
    scan = env.scan()
    high = env.row(scan, position=0, confidence="high", selected=False)
    low = env.row(scan, position=1, confidence="low", selected=False)
    picked_low = env.row(scan, position=2, confidence="low", selected=True)
    rejected = env.row(scan, position=3, state="rejected", confidence="high", selected=False)
    url = f"/api/photo-upgrade/scans/{scan}/select"

    resp = env.client.post(url, json={"which": "confident"})
    assert resp.status_code == 200 and resp.json()["selected"] == 2
    assert [env.sugg(r).selected for r in (high, low, picked_low, rejected)] == [
        True, False, True, False]

    resp = env.client.post(url, json={"which": "none"})
    assert resp.json()["selected"] == 0
    assert not any(env.sugg(r).selected for r in (high, low, picked_low, rejected))

    resp = env.client.post(url, json={"which": "all_proposed"})
    assert resp.json()["selected"] == 3
    assert [env.sugg(r).selected for r in (high, low, picked_low, rejected)] == [
        True, True, True, False]
    assert env.client.post(url, json={"which": "bogus"}).status_code == 422


# ── candidates ────────────────────────────────────────────────────────────────

def test_candidates_are_the_days_album_photos_ranked(env):
    mid, uuids = env.memory(1, seed=10)     # the trip photo is _pattern(10)
    scan = env.scan()
    row = env.row(scan, mid, uuids[0], asset_id=None, state="no_match")
    env.asset(scan, "other", img=_pattern(77, (400, 300)), local_date="2026-05-01")
    env.asset(scan, "same", img=_pattern(10, (400, 300)), local_date="2026-05-03")
    env.asset(scan, "far", img=_pattern(10, (400, 300)), local_date="2026-05-05")
    env.asset(scan, "unread", local_date=_DAY, fingerprinted=False)
    env.asset(scan, "portrait", img=_pattern(10, (300, 400)))

    resp = env.client.get(f"/api/photo-upgrade/scans/{scan}/candidates",
                          params={"suggestion_id": row})
    assert resp.status_code == 200
    got = resp.json()["candidates"]
    assert [c["asset_id"] for c in got] == ["same", "other"]
    assert got[0]["score"] < got[1]["score"]
    # The order and scores are rank_candidates' own.
    with Session(env.engine) as sess:
        assets = sess.exec(select(DBPhotoUpgradeAsset).where(
            DBPhotoUpgradeAsset.asset_id.in_(["same", "other"]))).all()
    thumb = (env.folder("memory", mid) / f"{uuids[0]}_thumb.jpg").read_bytes()
    expected = rank_candidates(
        TripPhoto(key="k", date=dt.date.fromisoformat(_DAY), lat=None, lon=None,
                  fp=fingerprint(thumb), pixels=None, source=None, content_hash=None),
        [_album_photo(a) for a in assets])
    assert [(c["asset_id"], c["score"]) for c in got] == expected
    assert (got[0]["width"], got[0]["height"], got[0]["mime"]) == (4000, 3000, "image/jpeg")


def test_candidates_without_a_readable_thumbnail_list_the_day_unranked(env):
    mid, uuids = env.memory(1)
    (env.folder("memory", mid) / f"{uuids[0]}_thumb.jpg").unlink()
    scan = env.scan()
    row = env.row(scan, mid, uuids[0], asset_id=None, state="no_match")
    env.asset(scan, "b", img=_pattern(2), local_date="2026-05-03")
    env.asset(scan, "a", img=_pattern(1), local_date="2026-05-01")

    got = env.client.get(f"/api/photo-upgrade/scans/{scan}/candidates",
                         params={"suggestion_id": row}).json()["candidates"]
    assert [(c["asset_id"], c["score"]) for c in got] == [("a", None), ("b", None)]


# ── apply ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("start", ["idle", "stopped", "done"])
def test_apply_starts_or_resumes(env, start):
    scan = env.scan(apply_status=start,
                    apply_stopped_reason="storage_full" if start == "stopped" else None)
    resp = env.client.post(f"/api/photo-upgrade/scans/{scan}/apply")
    assert resp.status_code == 202
    assert resp.json()["apply_status"] == "pending"
    got = env.get(scan)
    assert (got.apply_status, got.apply_stopped_reason) == ("pending", None)
    assert env.applies_started == [scan]


@pytest.mark.parametrize("status", ["pending", "scanning", "failed"])
def test_apply_of_a_scan_not_ready_is_409(env, status):
    scan = env.scan(status=status)
    resp = env.client.post(f"/api/photo-upgrade/scans/{scan}/apply")
    assert resp.status_code == 409 and _code(resp) == "scan_not_ready"
    assert env.applies_started == []


@pytest.mark.parametrize("apply_status", ["pending", "running"])
def test_a_scan_an_apply_or_a_delete_during_an_apply_is_409(env, apply_status):
    applying = env.scan(apply_status=apply_status)
    earlier = env.scan(created_at=time.time() - 1000)

    resp = env.client.post(env.url(), json=_scan_body())
    assert resp.status_code == 409 and _code(resp) == "apply_in_progress"
    resp = env.client.post(f"/api/photo-upgrade/scans/{applying}/apply")
    assert resp.status_code == 409 and _code(resp) == "apply_in_progress"
    resp = env.client.post(f"/api/photo-upgrade/scans/{earlier}/apply")
    assert resp.status_code == 409 and _code(resp) == "apply_in_progress"
    resp = env.client.delete(f"/api/photo-upgrade/scans/{applying}")
    assert resp.status_code == 409 and _code(resp) == "apply_in_progress"

    assert len(env.all_scans()) == 2
    assert env.get(earlier).apply_status == "idle"
    assert env.scans_started == [] and env.applies_started == []


@pytest.mark.parametrize("status", ["pending", "scanning"])
def test_apply_of_an_earlier_review_while_a_newer_scan_runs_is_409(env, status):
    earlier = env.scan(created_at=time.time() - 1000)
    env.scan(status=status)
    resp = env.client.post(f"/api/photo-upgrade/scans/{earlier}/apply")
    assert resp.status_code == 409 and _code(resp) == "scan_in_progress"
    assert env.get(earlier).apply_status == "idle"
    assert env.applies_started == []


def test_an_apply_racing_a_new_scan_is_refused_by_its_update(env, monkeypatch):
    """A scan inserted after the apply's reads: the conditional UPDATE sees it."""
    scan = env.scan()
    real = pu._refuse_if_busy

    def check_then_race(sess, project_id, user_info_id):
        real(sess, project_id, user_info_id)
        env.scan(user_info_id, status="pending")

    monkeypatch.setattr(pu, "_refuse_if_busy", check_then_race)
    resp = env.client.post(f"/api/photo-upgrade/scans/{scan}/apply")
    assert resp.status_code == 409
    assert env.get(scan).apply_status == "idle"
    assert env.applies_started == []


def test_a_companions_apply_does_not_block_the_owner(env):
    env.scan(env.mate, apply_status="running", include_memories=False)
    scan = env.scan()
    assert env.client.post(f"/api/photo-upgrade/scans/{scan}/apply").status_code == 202


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_discards_the_scan_and_its_rows(env):
    scan = env.scan()
    env.row(scan)
    env.asset(scan, "asset-1")
    assert env.client.delete(f"/api/photo-upgrade/scans/{scan}").status_code == 204
    assert env.get(scan) is None
    with Session(env.engine) as sess:
        assert sess.exec(select(DBPhotoUpgradeSuggestion)).all() == []
        assert sess.exec(select(DBPhotoUpgradeAsset)).all() == []


# ── The hourly sweep ──────────────────────────────────────────────────────────

#: "Now" for the sweep: just after every scan these tests create by default.
_NOW = time.time() + 60


_AGED = _NOW - pu.RETENTION_S - 1


def _rows_of(env, model, scan_id) -> list:
    with Session(env.engine) as sess:
        return sess.exec(select(model).where(model.scan_id == scan_id)).all()


def _rescan(env) -> int:
    """A new scan of the owner's memories, matched by the real scan job:
    its rejections come from ``_earlier_rejections``. Its album holds the
    frame of the trip photo ``env.memory(1)`` writes, as asset A."""
    scan = env.scan(status="scanning")
    env.asset(scan, "A", img=_pattern(0, (400, 300)))
    scan_job._match(scan)
    assert env.get(scan).status == "ready"
    return scan


def _row_for(env, scan_id, photo_uuid) -> DBPhotoUpgradeSuggestion:
    [row] = [r for r in _rows_of(env, DBPhotoUpgradeSuggestion, scan_id)
             if r.photo_uuid == photo_uuid]
    return row


def test_a_rescan_without_rejections_proposes_the_frame(env):
    """The control for the two tests below: frame A is P's match."""
    _, (p,) = env.memory(1)
    row = _row_for(env, _rescan(env), p)
    assert (row.state, row.asset_id, row.selected) == ("proposed", "A", True)


def test_the_sweep_expires_a_ready_scan_and_keeps_its_rejections(env):
    """U10-R1-1: a rejection outlives the 30 days."""
    mid, (p,) = env.memory(1)
    old = env.scan(created_at=_AGED)
    env.asset(old, "A", img=_pattern(0, (400, 300)))
    row = env.row(old, mid, p, asset_id="A")
    assert _patch(env, old, row, rejected=True).status_code == 200

    done = pu.sweep_photo_upgrade_scans(_NOW)

    assert (done["scans_expired"], done["scans_deleted"]) == (1, 0)
    got = env.get(old)
    assert got.status == "expired"
    assert json.loads(got.rejected_pairs_json) == [[photo_key("memory", mid, p), "A"]]
    assert _rows_of(env, DBPhotoUpgradeSuggestion, old) == []
    assert _rows_of(env, DBPhotoUpgradeAsset, old) == []

    # A rescan months later still honours it: A is not proposed for P.
    row = _row_for(env, _rescan(env), p)
    assert (row.state, row.selected) == ("rejected", False)


def test_an_unreject_on_the_newer_scan_survives_the_sweep(env):
    """The older scan rejected A for P; the newer one un-rejected it. Once
    both are cleared, A may be proposed again."""
    mid, (p,) = env.memory(1)
    key = photo_key("memory", mid, p)
    older = env.scan(created_at=_AGED - 1000, rejected_pairs_json=json.dumps([[key, "A"]]))
    env.row(older, mid, p, asset_id="A", state="rejected", selected=False)
    newer = env.scan(created_at=_AGED, rejected_pairs_json=json.dumps([[key, "A"]]))
    env.asset(newer, "A", img=_pattern(0, (400, 300)))
    row = env.row(newer, mid, p, asset_id="A", state="rejected", selected=False)
    assert _patch(env, newer, row, rejected=False).status_code == 200
    # remove_rejection took the pair off the newer scan's column.
    assert env.pairs(newer) == []

    done = pu.sweep_photo_upgrade_scans(_NOW)

    assert (done["scans_expired"], done["scans_deleted"]) == (2, 1)
    assert env.get(older) is None
    assert (env.get(newer).status, env.pairs(newer)) == ("expired", [])
    row = _row_for(env, _rescan(env), p)
    assert (row.state, row.asset_id) == ("proposed", "A")


def test_clearing_folds_the_verdicts_of_the_scans_it_supersedes(env):
    """A rejection only an earlier scan holds is kept on the expired one."""
    mid, (p,) = env.memory(1)
    key = photo_key("memory", mid, p)
    older = env.scan(created_at=_AGED - 1000)
    env.row(older, mid, p, asset_id="A", state="rejected", selected=False)
    newer = env.scan(created_at=_AGED - 10)
    env.row(newer, mid, p, asset_id="B", state="proposed")

    pu.sweep_photo_upgrade_scans(_NOW)

    assert env.get(older) is None
    assert env.pairs(newer) == [[key, "A"]]


def test_an_expired_scan_is_hidden_and_refused(env):
    mid, (p,) = env.memory(1)
    old = env.scan(created_at=_AGED)
    row = env.row(old, mid, p, asset_id="A", state="rejected", selected=False)
    pu.sweep_photo_upgrade_scans(_NOW)
    assert env.get(old).status == "expired"

    body = env.client.get(env.url("/latest")).json()
    assert (body["scan"], body["newer"]) == (None, None)
    base = f"/api/photo-upgrade/scans/{old}"
    for method, url, json_body in [
            ("get", f"{base}/suggestions", None),
            ("get", f"{base}/candidates?suggestion_id={row}", None),
            ("patch", f"{base}/suggestions/{row}", {"selected": False}),
            ("post", f"{base}/select", {"which": "none"}),
            ("post", f"{base}/apply", None)]:
        kw = {"json": json_body} if json_body is not None else {}
        resp = getattr(env.client, method)(url, **kw)
        assert resp.status_code == 409 and _code(resp) == "scan_not_ready", (method, url)
    assert env.applies_started == []

    # It is not active: a new scan starts beside it.
    assert env.client.post(env.url(), json=_scan_body()).status_code == 202


def test_an_expired_scan_goes_with_the_trip_and_with_the_account(env):
    with Session(env.engine) as sess:
        other = DBProject(user_info_id=env.owner, name="Other")
        sess.add(other)
        sess.commit()
        sess.add(DBProjectMember(project_id=other.id, user_info_id=env.mate, role="editor",
                                 invited_by=env.owner))
        sess.commit()
        other_id = other.id
    trip = env.scan(status="expired")
    mates = env.scan(env.mate, status="expired", include_memories=False)
    with Session(env.engine) as sess:
        row = DBPhotoUpgradeScan(project_id=other_id, user_info_id=env.mate, album_id="a",
                                 status="expired")
        sess.add(row)
        sess.commit()
        elsewhere = row.id

    with Session(env.engine) as sess:
        assert ProjectRepo().delete_project(sess, env.owner, "Trip") is True
    assert env.get(trip) is None and env.get(mates) is None
    with Session(env.engine) as sess:
        delete_user_and_data(sess, env.mate)
    assert env.get(elsewhere) is None


@pytest.mark.parametrize("apply_status", ["pending", "running"])
def test_an_aged_scan_with_an_apply_in_progress_is_untouched(env, apply_status):
    old = env.scan(created_at=_AGED, apply_status=apply_status, apply_heartbeat_at=_NOW - 60)
    env.row(old)
    env.asset(old, "A")

    pu.sweep_photo_upgrade_scans(_NOW)

    assert (env.get(old).status, env.get(old).apply_status) == ("ready", apply_status)
    assert len(_rows_of(env, DBPhotoUpgradeSuggestion, old)) == 1
    assert len(_rows_of(env, DBPhotoUpgradeAsset, old)) == 1


def test_an_aged_scan_waits_while_an_earlier_one_is_applying(env):
    """Clearing it would delete the earlier scan, which cannot go yet."""
    earlier = env.scan(created_at=_AGED - 1000, apply_status="running",
                       apply_heartbeat_at=_NOW - 60)
    later = env.scan(created_at=_AGED)
    env.row(later)

    pu.sweep_photo_upgrade_scans(_NOW)
    assert env.get(later).status == "ready"
    assert env.get(earlier) is not None

    with Session(env.engine) as sess:
        s = sess.get(DBPhotoUpgradeScan, earlier)
        s.apply_status = "done"
        sess.add(s)
        sess.commit()
    pu.sweep_photo_upgrade_scans(_NOW)
    assert env.get(later).status == "expired"
    assert env.get(earlier) is None


def test_a_second_sweep_changes_nothing(env):
    mid, (p,) = env.memory(1)
    old = env.scan(created_at=_AGED)
    row = env.row(old, mid, p, asset_id="A")
    assert _patch(env, old, row, rejected=True).status_code == 200
    young = env.scan(created_at=_NOW - 3600, status="failed")
    pu.sweep_photo_upgrade_scans(_NOW)

    def snapshot():
        return [(s.id, s.status, s.apply_status, s.rejected_pairs_json, s.completed_at)
                for s in env.all_scans()]

    before = snapshot()
    assert pu.sweep_photo_upgrade_scans(_NOW) == {
        "scans_failed": 0, "applies_stopped": 0, "scans_expired": 0, "scans_deleted": 0}
    assert snapshot() == before
    assert {s[0] for s in before} == {old, young}


@pytest.mark.parametrize("status", ["failed", "pending", "scanning"])
def test_an_aged_scan_that_never_became_ready_is_deleted(env, status):
    old = env.scan(status=status, created_at=_AGED, heartbeat_at=_NOW - 60)
    env.row(old)
    young = env.scan(env.mate, status="failed", created_at=_NOW - 3600,
                     include_memories=False)

    pu.sweep_photo_upgrade_scans(_NOW)

    assert env.get(old) is None
    assert _rows_of(env, DBPhotoUpgradeSuggestion, old) == []
    assert env.get(young) is not None


def test_the_sweep_fails_scans_pending_for_24_hours(env):
    stale = env.scan(status="pending", created_at=_NOW - pu.STALE_PENDING_S - 1)
    waiting = env.scan(env.mate, status="pending", created_at=_NOW - 3600,
                       include_memories=False)

    pu.sweep_photo_upgrade_scans(_NOW)

    got = env.get(stale)
    assert (got.status, got.error_message) == ("failed", pu.REASON_NEVER_STARTED)
    assert env.get(waiting).status == "pending"


def test_the_sweep_fails_scans_whose_heartbeat_stopped(env):
    dead = env.scan(status="scanning", heartbeat_at=_NOW - pu.STALE_HEARTBEAT_S - 1,
                    created_at=_NOW - 7200)
    alive = env.scan(env.mate, status="scanning", heartbeat_at=_NOW - 60,
                     created_at=_NOW - 7200, include_memories=False)

    pu.sweep_photo_upgrade_scans(_NOW)

    got = env.get(dead)
    assert got.status == "failed"
    assert got.error_message == "The check of your album was interrupted. Please start it again."
    assert env.get(alive).status == "scanning"


def test_the_sweep_stops_applies_pending_for_24_hours(env):
    stale = env.scan(apply_status="pending", apply_heartbeat_at=_NOW - pu.STALE_PENDING_S - 1,
                     created_at=_NOW - pu.STALE_PENDING_S - 10)
    waiting = env.scan(env.mate, apply_status="pending", apply_heartbeat_at=_NOW - 3600,
                       include_memories=False)

    done = pu.sweep_photo_upgrade_scans(_NOW)

    assert done["applies_stopped"] == 1
    got = env.get(stale)
    assert (got.apply_status, got.apply_stopped_reason) == ("stopped", "interrupted")
    assert env.get(waiting).apply_status == "pending"


def test_the_sweep_recovers_and_stops_applies_whose_heartbeat_stopped(env):
    mid, uuids = env.memory(1)
    dead = env.scan(apply_status="running", apply_heartbeat_at=_NOW - pu.STALE_HEARTBEAT_S - 1)
    left = env.row(dead, mid, uuids[0], state="applying")
    alive = env.scan(env.mate, apply_status="running", apply_heartbeat_at=_NOW - 60,
                     include_memories=False)
    taken = env.row(alive, state="applying", kind="journal")

    pu.sweep_photo_upgrade_scans(_NOW)

    got = env.get(dead)
    assert (got.apply_status, got.apply_stopped_reason) == ("stopped", "interrupted")
    # Its photo is still listed: the replace never happened.
    assert env.sugg(left).state == "proposed"
    assert env.get(alive).apply_status == "running"
    assert env.sugg(taken).state == "applying"


def test_the_sweep_never_raises(env, monkeypatch):
    def broken():
        raise RuntimeError("database is gone")
    monkeypatch.setattr(pu, "get_session", broken)
    assert pu.sweep_photo_upgrade_scans(_NOW) == {
        "scans_failed": 0, "applies_stopped": 0, "scans_expired": 0, "scans_deleted": 0}


def test_the_api_schedules_the_sweep_hourly(monkeypatch):
    import api.router as router

    jobs = {}

    class _Scheduler:
        def add_job(self, func, trigger, **kwargs):
            jobs[kwargs["id"]] = (func, trigger, kwargs)
        def add_listener(self, *a, **k): pass
        def start(self): pass
        def shutdown(self, *a, **k): pass

    monkeypatch.setattr(router, "_IS_API_PROCESS", True)
    monkeypatch.setattr(router.alembic_command, "upgrade", lambda *a, **k: None)
    monkeypatch.setattr(router, "_check_schema_contract", lambda: None)
    monkeypatch.setattr(router, "seed_admin", lambda: None)
    monkeypatch.setattr(router, "sweep_orphaned_jobs", lambda: 0)
    monkeypatch.setattr(router, "sweep_orphaned_poster_jobs", lambda: 0)
    monkeypatch.setattr(router, "move_companion_avatars", lambda: None)
    monkeypatch.setattr(router, "_scheduler", _Scheduler())

    with TestClient(router.app):
        pass
    func, trigger, kwargs = jobs["photo_upgrade_sweep"]
    assert func is pu.sweep_photo_upgrade_scans and trigger == "cron"
    assert set(kwargs) - {"id", "replace_existing"} == {"minute"}
    taken = {kw.get("minute") for name, (_, _, kw) in jobs.items()
             if name != "photo_upgrade_sweep" and set(kw) - {"id", "replace_existing"}
             == {"minute"}}
    assert kwargs["minute"] not in taken
