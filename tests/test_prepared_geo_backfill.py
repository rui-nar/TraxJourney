"""The backfill sweep for prepared activity geometry — issue #369 stage B2.

Activities written since #369 get their prepared blob beside the polyline. Ones
that predate it have no row and would otherwise be prepared lazily on a user's
first open of the trip — correct, but it means the first open still pays the
cost the change exists to remove. This sweep drains that backlog in the
background.

What these pin is that it is *bounded*, *terminates*, *skips what it cannot
prepare*, and — the one that matters most — that it cannot overwrite a writer
that landed while it was preparing.
"""
from __future__ import annotations

import json

import polyline as polyline_lib
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
import src.jobs.prepared_geo_jobs as jobs
from models.project_db import DBActivity, DBActivityGeoPrepared
from src.models.prepared_geo import unpack_prepared_line
from src.models.simplify import PREPARED_GEO_VERSION


def _track(seed: int, n: int = 60):
    return [(45.0 + seed * 0.01 + i * 0.0001, 7.0 + i * 0.0001) for i in range(n)]


def _encoded(seed: int, n: int = 60) -> str:
    return polyline_lib.encode(_track(seed, n))


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    # The resume cursor is module state; a test that inherited another's would
    # start mid-range and silently skip rows.
    monkeypatch.setattr(jobs, "_resume_after_id", None)
    return engine


def _add(engine, activity_id: int, polyline: str | None):
    with Session(engine) as sess:
        sess.add(DBActivity(
            id=activity_id, user_info_id=1, name=f"A{activity_id}", type="Ride",
            start_date="2026-06-01T00:00:00Z", summary_polyline=polyline,
            start_latlng_json=json.dumps([45.0, 7.0]),
        ))
        sess.commit()


def _rows(engine) -> dict[int, DBActivityGeoPrepared]:
    with Session(engine) as sess:
        return {r.activity_id: r for r in sess.exec(select(DBActivityGeoPrepared)).all()}


def test_it_prepares_activities_that_have_no_row(db):
    for i in range(3):
        _add(db, 100 + i, _encoded(i))

    assert jobs.sweep_unprepared_geometry() == 3
    rows = _rows(db)
    assert set(rows) == {100, 101, 102}
    assert all(r.version == PREPARED_GEO_VERSION for r in rows.values())
    # A real blob, not a placeholder.
    points, levels, _bbox, version = unpack_prepared_line(rows[100].blob)
    assert version == PREPARED_GEO_VERSION
    assert len(levels) == len(points) // 2


def test_it_is_bounded_per_run_and_terminates(db):
    for i in range(7):
        _add(db, 200 + i, _encoded(i))

    assert jobs.sweep_unprepared_geometry(limit=3) == 3
    assert jobs.sweep_unprepared_geometry(limit=3) == 3
    assert jobs.sweep_unprepared_geometry(limit=3) == 1
    # Drained: further runs are no-ops, which is what lets this sit on a
    # five-minute schedule forever.
    assert jobs.sweep_unprepared_geometry(limit=3) == 0


def test_a_second_run_does_not_redo_the_first(db):
    _add(db, 300, _encoded(1))
    assert jobs.sweep_unprepared_geometry() == 1
    before = _rows(db)[300].blob
    assert jobs.sweep_unprepared_geometry() == 0
    assert _rows(db)[300].blob == before


def test_it_skips_an_encrypted_polyline(db):
    """The server holds no key, so there is nothing to prepare — and a row would
    be worse than none: the reader would serve it."""
    _add(db, 400, "v1.YWJj.ZGVm")
    _add(db, 401, _encoded(2))
    assert jobs.sweep_unprepared_geometry() == 1
    assert set(_rows(db)) == {401}


def test_it_skips_an_activity_with_no_polyline(db):
    _add(db, 500, None)
    assert jobs.sweep_unprepared_geometry() == 0
    assert _rows(db) == {}


def test_one_undecodable_row_does_not_cost_the_batch(db):
    _add(db, 600, "!!!not a polyline!!!")
    _add(db, 601, _encoded(3))
    # The bad row is logged and skipped; the good one is still prepared.
    assert jobs.sweep_unprepared_geometry() == 1
    assert set(_rows(db)) == {601}


def test_a_stale_version_row_is_refreshed(db):
    _add(db, 700, _encoded(4))
    with Session(db) as sess:
        sess.add(DBActivityGeoPrepared(
            activity_id=700, version=PREPARED_GEO_VERSION - 1, blob=b"stale"))
        sess.commit()

    assert jobs.sweep_unprepared_geometry() == 1
    row = _rows(db)[700]
    assert row.version == PREPARED_GEO_VERSION
    assert row.blob != b"stale"


def test_it_cannot_overwrite_a_write_that_landed_while_it_prepared(db, monkeypatch):
    """The race the read path was caught by, in the sweep's own shape.

    Preparation happens outside any transaction, so a writer can change the
    polyline in between. Without the compare-and-swap the sweep would store
    geometry built from the *old* value at the current version — indistinguishable
    from fresh, and served until the next polyline write.
    """
    _add(db, 800, _encoded(5))
    real_prepare = jobs.prepare_polyline

    def prepare_then_let_a_writer_in(polyline):
        blob = real_prepare(polyline)
        with Session(db) as sess:
            row = sess.get(DBActivity, 800)
            row.summary_polyline = _encoded(99)
            sess.add(row)
            sess.commit()
        return blob

    monkeypatch.setattr(jobs, "prepare_polyline", prepare_then_let_a_writer_in)

    jobs.sweep_unprepared_geometry()
    # The guard refused the write, so no row was left claiming to be current for
    # geometry that is no longer the activity's.
    assert _rows(db) == {}

    # And the next sweep, seeing the new polyline, prepares it correctly.
    monkeypatch.setattr(jobs, "prepare_polyline", real_prepare)
    assert jobs.sweep_unprepared_geometry() == 1
    assert _rows(db)[800].version == PREPARED_GEO_VERSION


def test_unpreparable_rows_at_the_head_cannot_starve_the_backlog(db):
    """The blocking finding from the review of #413/#414.

    A row that can never be prepared never gets a prepared row, so it stays a
    candidate forever. The first version ordered by id with a LIMIT and no
    cursor, so enough of them at the head of the id order meant every run read
    the same rows, prepared nothing, and never reached the backlog behind them —
    silently, since it only logged when it made progress.

    Single-point tracks rather than encrypted ones on purpose: envelopes are now
    filtered in SQL, so they would pass this test for the wrong reason. A track
    with one GPS fix is the realistic unpreparable row SQL cannot see — it
    decodes fine and yields nothing drawable — which is exactly what the cursor
    is for.

    (Not a garbage string: polyline.decode is lenient enough that most garbage
    decodes to a few points and prepares successfully, which is how the first
    draft of this test passed for the wrong reason.)
    """
    single_fix = polyline_lib.encode([(45.0, 7.0)])
    for i in range(4):
        _add(db, 1000 + i, single_fix)
    _add(db, 2000, _encoded(7))

    per_run = [jobs.sweep_unprepared_geometry(limit=2) for _ in range(4)]

    assert 2000 in _rows(db), f"good row never reached; prepared per run: {per_run}"
    assert sum(per_run) == 1


def test_the_cursor_wraps_so_a_later_pass_starts_over(db):
    """A short batch means the end was reached; the next run begins again."""
    for i in range(3):
        _add(db, 3000 + i, _encoded(i))

    assert jobs.sweep_unprepared_geometry(limit=2) == 2  # full batch: resumes after
    assert jobs.sweep_unprepared_geometry(limit=2) == 1  # short batch: wraps
    assert jobs._resume_after_id is None


def test_an_encrypted_envelope_is_never_read(db, monkeypatch):
    """Filtered in SQL, so its ciphertext never reaches prepare_polyline at all."""
    _add(db, 4000, "v1.YWJj.ZGVm")
    _add(db, 4001, _encoded(8))
    seen = []
    real = jobs.prepare_polyline
    monkeypatch.setattr(jobs, "prepare_polyline",
                        lambda poly: (seen.append(poly), real(poly))[1])

    jobs.sweep_unprepared_geometry()

    assert "v1.YWJj.ZGVm" not in seen
    assert set(_rows(db)) == {4001}


def test_the_envelope_filter_cannot_exclude_a_real_polyline():
    """The SQL filter's safety argument, checked rather than asserted.

    `NOT LIKE 'v1.%'` is only safe if no encoded polyline can begin with "v1.".
    The encoding's alphabet is ASCII 63-126, which contains neither "1" nor ".".
    """
    import random
    for seed in range(500):
        rng = random.Random(seed)
        pts = [(rng.uniform(-90, 90), rng.uniform(-180, 180))
               for _ in range(rng.randint(1, 30))]
        encoded = polyline_lib.encode(pts)
        assert not encoded.lower().startswith("v1."), encoded
        assert "1" not in encoded and "." not in encoded


def test_app_created_activities_with_negative_ids_are_reached(db):
    """Blocking finding from the re-review of the cursor fix.

    Activities the app creates itself — GPX imports and split tails — take
    negative ids (src/project/local_ids.py). The first version of the cursor
    started and wrapped at 0 and filtered `id > cursor`, so it never saw any of
    them, silently, even though they are exactly the legacy rows this sweep
    exists for. The version before the cursor did reach them; the fix had
    regressed it.
    """
    _add(db, -4503599627370495, _encoded(11))  # a GPX import's id range
    _add(db, -7, _encoded(12))                  # a split tail's dense range
    _add(db, 5, _encoded(13))                   # a Strava activity

    for _ in range(3):
        jobs.sweep_unprepared_geometry(limit=2)

    assert set(_rows(db)) == {-4503599627370495, -7, 5}


def test_a_real_track_beginning_with_v_is_not_mistaken_for_an_envelope(db):
    """The SQL filter must be exactly as wide as claimed, and no wider.

    `NOT LIKE 'v1.%'` is safe only because no encoded polyline can begin with
    "v1.". But a filter widened to `v%` would still pass a test that only checks
    envelopes are excluded — and about 3% of real encoded tracks begin with "v".
    This pins the other side: a genuine track starting with "v" is prepared.
    """
    pts = [(-26.45004, -9.77305), (5.66769, -95.63986), (0.26578, 109.52882)]
    encoded = polyline_lib.encode(pts)
    assert encoded.startswith("v"), "fixture no longer exercises the case"
    _add(db, 900, encoded)

    assert jobs.sweep_unprepared_geometry() == 1
    assert set(_rows(db)) == {900}


def test_a_full_batch_does_not_refetch_its_last_row(db, monkeypatch):
    """The cursor is exclusive: `id > cursor`, not `>=`.

    An inclusive cursor still makes progress, which is why nothing else catches
    it — but it re-reads the boundary row on every pass.
    """
    for i in range(4):
        _add(db, 5000 + i, polyline_lib.encode([(45.0, 7.0)]))  # unpreparable
    seen: list[int] = []
    real = jobs._candidates

    def recording(sess, limit, after_id):
        rows = real(sess, limit, after_id)
        seen.extend(r[0] for r in rows)
        return rows

    monkeypatch.setattr(jobs, "_candidates", recording)
    jobs.sweep_unprepared_geometry(limit=2)
    jobs.sweep_unprepared_geometry(limit=2)

    assert seen == [5000, 5001, 5002, 5003]


def test_a_zero_limit_is_a_no_op_rather_than_a_crash(db):
    _add(db, 6000, _encoded(14))
    assert jobs.sweep_unprepared_geometry(limit=0) == 0
    assert _rows(db) == {}


def test_the_cursor_starts_from_the_whole_id_range():
    """The module default must be None — deliberately NOT using the `db` fixture.

    The fixture resets the cursor before every test, which is right for isolation
    but hides a wrong *default*: with it in place, a cursor initialised to 0 is
    overwritten before any test can see it, and the negative-id test above still
    passes. Production has no fixture. Checked on its own for that reason.
    """
    assert jobs._resume_after_id is None


def test_negative_ids_are_reached_after_the_cursor_wraps(db):
    """A pass that has moved past every negative id must reach them on the next.

    Sets the cursor where an in-progress pass would leave it — above the negative
    range — so the only way back to row -7 is through the wrap. A wrap to 0
    instead of None skips the whole negative range on every pass after the first.
    """
    _add(db, -7, _encoded(15))
    _add(db, 10, _encoded(16))
    jobs._resume_after_id = 5  # mid-pass, already beyond the negative ids

    jobs.sweep_unprepared_geometry(limit=2)  # finds only 10: short batch, wraps
    jobs.sweep_unprepared_geometry(limit=2)  # must start over and find -7

    assert set(_rows(db)) == {-7, 10}


# ── observability: a stuck sweep must be visible (review of 44e8a8d) ─────────

import logging

from prometheus_client import REGISTRY


def _backlog():
    return REGISTRY.get_sample_value("traxjourney_prepared_geometry_backlog")


def _outcomes(outcome: str) -> float:
    return REGISTRY.get_sample_value(
        "traxjourney_prepared_geometry_outcomes_total", {"outcome": outcome}) or 0.0


def test_the_backlog_gauge_is_published_and_falls(db):
    for i in range(3):
        _add(db, 7000 + i, _encoded(20 + i))
    jobs.sweep_unprepared_geometry(limit=2)
    assert _backlog() == 1
    jobs.sweep_unprepared_geometry(limit=2)
    assert _backlog() == 0


def test_the_gauge_is_published_even_when_a_run_finds_nothing(db, monkeypatch):
    """The path that matters most, and the one first written wrong.

    The previous fix's regression — a cursor that skipped every negative id —
    produced runs that found NO candidates and returned early. An early return
    that skipped the gauge would leave it holding a stale value from a healthy
    past run, and the sweep would look fine. So a run with no candidates must
    still publish the true backlog.
    """
    _add(db, -9, _encoded(30))
    PREPARED_GEOMETRY_BACKLOG_STALE = 0
    from src.utils.metrics import PREPARED_GEOMETRY_BACKLOG
    PREPARED_GEOMETRY_BACKLOG.set(PREPARED_GEOMETRY_BACKLOG_STALE)

    # Simulate the regressed cursor: it believes it is past everything.
    monkeypatch.setattr(jobs, "_candidates", lambda sess, limit, after_id: [])
    assert jobs.sweep_unprepared_geometry() == 0

    assert _backlog() == 1, "a run that found nothing left the gauge stale"


def test_a_stuck_sweep_shows_as_a_flat_nonzero_backlog(db, monkeypatch):
    """The failure this job shipped twice, replayed, must be visible.

    Replays the original starvation bug — a cursor that never advances past
    unpreparable rows at the head — and checks the gauge: it must stay non-zero
    and flat across runs rather than falling, which is what a dashboard or alert
    can see even though every run reports success.
    """
    single_fix = polyline_lib.encode([(45.0, 7.0)])
    for i in range(3):
        _add(db, 8000 + i, single_fix)
    _add(db, 9000, _encoded(31))
    monkeypatch.setattr(jobs, "_resume_after_id", None)
    real = jobs._candidates
    # KNOWN-BAD cursor: always restart from the head, so the good row is never reached.
    monkeypatch.setattr(jobs, "_candidates",
                        lambda sess, limit, after_id: real(sess, limit, None))

    readings = []
    for _ in range(4):
        jobs.sweep_unprepared_geometry(limit=2)
        readings.append(_backlog())

    assert 9000 not in _rows(db)
    assert readings == [4, 4, 4, 4], f"stuck sweep not visible in the gauge: {readings}"


def test_an_undecodable_polyline_is_unpreparable_not_an_error(db, caplog):
    """It fails identically every pass forever, so it must not log a traceback."""
    _add(db, 9100, "!!!not a polyline!!!")  # raises OverflowError in decode
    before = _outcomes("unpreparable")
    with caplog.at_level(logging.ERROR):
        jobs.sweep_unprepared_geometry()
    assert _outcomes("unpreparable") == before + 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_failed_write_is_counted_as_an_error_and_logged(db, monkeypatch, caplog):
    _add(db, 9200, _encoded(32))
    before = _outcomes("error")

    def boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(jobs, "store_prepared_if_unchanged", boom)
    with caplog.at_level(logging.ERROR):
        assert jobs.sweep_unprepared_geometry() == 0
    assert _outcomes("error") == before + 1
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_failed_backlog_count_does_not_fail_a_successful_run(db, monkeypatch, caplog):
    """The rows were stored; a count that fails afterwards must not undo that.

    Written because the commit that introduced the `try` claimed it was covered
    when it was not — replacing its `except` with one that never fires passed
    every test.
    """
    _add(db, 9300, _encoded(33))
    real_exec_session = jobs.get_session
    calls = {"n": 0}

    from contextlib import contextmanager

    @contextmanager
    def session_that_fails_on_the_count():
        calls["n"] += 1
        with real_exec_session() as sess:
            if calls["n"] >= 3:  # 1: candidates, 2: the write, 3: the count
                raise RuntimeError("no such column: summary_polyline")
            yield sess

    monkeypatch.setattr(jobs, "get_session", session_that_fails_on_the_count)
    with caplog.at_level(logging.ERROR):
        assert jobs.sweep_unprepared_geometry() == 1
    assert 9300 in _rows(db)
    assert any("could not count" in r.getMessage() and r.exc_info
               for r in caplog.records), "a broken count must log with a traceback"
