"""Trip video timeline: legs, clips, pacing and sampling (docs/TRIP_VIDEO_PLAN.md, U1)."""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import polyline as polyline_lib
import pytest

from src.models.activity import Activity
from src.models.project import ConnectingSegment, Project, ProjectItem, SegmentEndpoint
from src.video.__main__ import format_timeline
from src.video.legs import Leg, LegSet, build_legs, feature_coords, prefix_km
from src.video.pacing import (
    END_S,
    MIN_CLIP_S,
    TITLE_S,
    Clip,
    SqrtBudgetPolicy,
    clip_budget_s,
    group_clips,
    max_clips,
    merge_same_day,
)
from src.video.timeline import (
    NothingToAnimate,
    build_timeline,
    ease_clock,
    timeline_for_project,
)

ENVELOPE = "v1.d3JhcHBlZA.Y2lwaGVy"


# ── builders ─────────────────────────────────────────────────────────────────

def _activity(id, *, type="Ride", day=date(2026, 5, 1), distance=20_000.0,
              moving_time=3600, elapsed_time=4000, polyline=None,
              start=(45.0, 6.0), end=(45.1, 6.1), **kw) -> Activity:
    when = datetime(day.year, day.month, day.day, 9, 0)
    return Activity(
        id=id, name=f"act {id}", type=type, distance=distance,
        moving_time=moving_time, elapsed_time=elapsed_time,
        total_elevation_gain=0.0, start_date=when, start_date_local=when,
        timezone="UTC", achievement_count=0, kudos_count=0, comment_count=0,
        athlete_count=1, photo_count=0, trainer=False, commute=False,
        manual=False, private=False, flagged=False, average_speed=0.0,
        max_speed=0.0, pr_count=0, total_photo_count=0,
        has_kudoed=False, summary_polyline=polyline,
        start_latlng=list(start) if start else None,
        end_latlng=list(end) if end else None, **kw)


def _segment(seg_type="flight", *, day=None, start=(48.85, 2.35), end=(41.9, 12.5),
             **kw) -> ConnectingSegment:
    return ConnectingSegment(
        id=f"seg-{seg_type}-{day}-{start}", segment_type=seg_type,
        start=SegmentEndpoint(*start), end=SegmentEndpoint(*end),
        date=day.isoformat() if day else None, **kw)


def _project(*things, trip_start=None) -> Project:
    items, activities = [], []
    for t in things:
        if isinstance(t, Activity):
            activities.append(t)
            items.append(ProjectItem(item_type="activity", activity_id=t.id))
        else:
            items.append(ProjectItem(item_type="segment", segment=t))
    return Project(name="Trip", items=items, activities=activities, trip_start=trip_start)


def _leg(i, *, mode="ride", real_s=3600.0, day=date(2026, 5, 1), km=None,
         lon0=None) -> Leg:
    lon0 = float(i) * 0.1 if lon0 is None else lon0
    coords = ((lon0, 45.0), (lon0 + 0.05, 45.02), (lon0 + 0.1, 45.0))
    cum = prefix_km(coords)
    return Leg(index=i, kind="activity", ref=i, label=f"leg {i}", mode=mode,
               coords=coords, cum_km=cum, km=cum[-1] if km is None else km,
               real_s=real_s, speed_kmh=20.0, date=day)


_MODES = ("ride", "hike", "run", "train", "ride", "bus", "flight", "boat")


def _big_trip() -> Project:
    """365 days, 1,000 items, every mode; deterministic."""
    start = date(2026, 1, 1)
    things = []
    for i in range(1000):
        day = start + timedelta(days=(i * 365) // 1000)
        mode = _MODES[(i * 7) % len(_MODES)]
        lat, lon = 40.0 + (i % 50) * 0.1, -5.0 + (i % 97) * 0.2
        if mode in ("train", "bus", "flight", "boat"):
            things.append(_segment(mode, day=day, start=(lat, lon), end=(lat + 0.5, lon + 0.7)))
        else:
            pts = [(lat + k * 0.001, lon + k * 0.0015) for k in range(30)]
            things.append(_activity(
                i + 1, type={"ride": "Ride", "hike": "Hike", "run": "Run"}[mode],
                day=day, distance=1000.0 * (1 + i % 40),
                moving_time=600 + (i * 131) % 20_000,
                polyline=polyline_lib.encode(pts)))
    return _project(*things)


# ── the shared geometry helper ───────────────────────────────────────────────

def test_feature_coords_activity_polyline_fallback_and_encrypted():
    pts = [(45.0, 6.0), (45.01, 6.02), (45.02, 6.05)]
    a = _activity(1, polyline=polyline_lib.encode(pts))
    assert feature_coords(a) == [[lon, lat] for lat, lon in polyline_lib.decode(a.summary_polyline)]
    # No polyline: straight line from start to end.
    assert feature_coords(_activity(2)) == [[6.0, 45.0], [6.1, 45.1]]
    assert feature_coords(_activity(3, start=None, end=None)) is None
    assert feature_coords(_activity(4, polyline=ENVELOPE)) is None
    # The consent geometry replaces an unreadable polyline.
    enc = _activity(5, polyline=ENVELOPE, start=None, end=None)
    assert feature_coords(enc, polyline_lib.encode(pts)) == [[lon, lat] for lat, lon in
                                                            polyline_lib.decode(polyline_lib.encode(pts))]
    # A polyline of one point is nothing to draw — no fallback to the endpoints.
    assert feature_coords(_activity(6, polyline=polyline_lib.encode([(45.0, 6.0)]))) is None


def test_feature_coords_segment_route_and_arc():
    route = [[2.35, 48.85], [3.0, 48.0], [4.8, 45.7]]
    rail = _segment("train", route_mode="rail", route_polyline=json.dumps(route))
    assert feature_coords(rail) == route
    arc = feature_coords(_segment("flight"))
    assert len(arc) == 50 and arc[0] == pytest.approx([2.35, 48.85])
    # A rail route mode without a resolved route falls back to the arc.
    assert len(feature_coords(_segment("train", route_mode="rail"))) == 50


# ── legs ─────────────────────────────────────────────────────────────────────

def test_leg_real_duration_precedence_and_speed():
    moving = _activity(1, moving_time=3600, elapsed_time=5000, distance=20_000)
    elapsed = _activity(2, moving_time=0, elapsed_time=7200, distance=20_000)
    neither = _activity(3, type="Hike", moving_time=0, elapsed_time=0, distance=9_000)
    legs = build_legs(_project(moving, elapsed, neither)).legs
    assert [leg.real_s for leg in legs] == [3600.0, 7200.0, pytest.approx(9.0 / 4.5 * 3600)]
    assert legs[0].speed_kmh == pytest.approx(20.0)
    assert legs[1].speed_kmh == pytest.approx(10.0)
    assert [leg.mode for leg in legs] == ["ride", "ride", "hike"]
    assert legs[0].km == 20.0


def test_segment_leg_uses_real_speed_and_env_override(monkeypatch):
    seg = _segment("train", day=date(2026, 5, 2))
    leg = build_legs(_project(seg)).legs[0]
    assert leg.speed_kmh == 100.0
    assert leg.km == pytest.approx(leg.cum_km[-1])
    assert leg.real_s == pytest.approx(leg.km / 100.0 * 3600)
    monkeypatch.setenv("VIDEO_SPEED_KMH_TRAIN", "200")
    assert build_legs(_project(seg)).legs[0].real_s == pytest.approx(leg.real_s / 2)
    monkeypatch.setenv("VIDEO_SPEED_KMH_TRAIN", "nonsense")
    assert build_legs(_project(seg)).legs[0].speed_kmh == 100.0


def test_skipped_items_are_reported():
    ok = _activity(1)
    no_geo = _activity(2, start=None, end=None)
    encrypted = _activity(3, polyline=ENVELOPE, start=None, end=None)
    enc_endpoints = _activity(4, start=None, end=None, start_latlng_enc=ENVELOPE,
                              end_latlng_enc=ENVELOPE)
    p = _project(ok, no_geo, encrypted, enc_endpoints)
    p.items.append(ProjectItem(item_type="activity", activity_id=99))
    legset = build_legs(p)
    assert [leg.ref for leg in legset.legs] == [1]
    assert [(s.ref, s.reason) for s in legset.skipped] == [
        (2, "no_geometry"), (3, "encrypted"), (4, "encrypted"), (99, "missing")]
    # With consent geometry, both encrypted activities become legs.
    line = polyline_lib.encode([(45.0, 6.0), (45.2, 6.3)])
    legset = build_legs(p, {3: line, 4: line})
    assert [leg.ref for leg in legset.legs] == [1, 3, 4]
    assert [leg.index for leg in legset.legs] == [0, 1, 2]


def test_undated_segment_takes_the_previous_leg_date():
    first = _segment("flight", start=(1.0, 1.0), end=(2.0, 2.0))
    a = _activity(1, day=date(2026, 5, 3))
    b = _segment("train", start=(3.0, 3.0), end=(4.0, 4.0))
    legs = build_legs(_project(first, a, b)).legs
    assert [leg.date for leg in legs] == [date(2026, 5, 3)] * 3


# ── clips ────────────────────────────────────────────────────────────────────

def test_max_clips_per_length():
    assert [max_clips(t) for t in (30, 60, 90)] == [16, 36, 56]


def test_same_mode_same_date_legs_merge():
    d1, d2 = date(2026, 5, 1), date(2026, 5, 2)
    legs = [_leg(0, mode="hike", day=d1), _leg(1, mode="hike", day=d1),
            _leg(2, mode="hike", day=d1), _leg(3, mode="train", day=d1),
            _leg(4, mode="hike", day=d1), _leg(5, mode="hike", day=d2)]
    groups = merge_same_day(legs)
    assert [[leg.index for leg in g] for g in groups] == [[0, 1, 2], [3], [4], [5]]
    clips = group_clips(legs, n_max=36)
    assert [[leg.index for leg in c.legs] for c in clips] == [[0, 1, 2], [3], [4], [5]]


def test_adjacent_merge_prefers_same_mode_then_leftmost():
    days = [date(2026, 5, 1) + timedelta(days=i) for i in range(4)]
    # Four equal-weight legs; only 1-2 share a mode, so they merge first.
    legs = [_leg(0, mode="ride", day=days[0]), _leg(1, mode="train", day=days[1]),
            _leg(2, mode="train", day=days[2]), _leg(3, mode="hike", day=days[3])]
    assert [[l.index for l in c.legs] for c in group_clips(legs, 3)] == [[0], [1, 2], [3]]
    # All different modes and equal weights: the leftmost pair merges.
    legs = [_leg(i, mode=m, day=days[i]) for i, m in enumerate(("ride", "train", "hike", "bus"))]
    assert [[l.index for l in c.legs] for c in group_clips(legs, 3)] == [[0, 1], [2], [3]]


def test_clip_keeps_each_leg_mode():
    legs = [_leg(0, mode="ride", day=date(2026, 5, 1)),
            _leg(1, mode="train", day=date(2026, 5, 2), real_s=7200)]
    (clip,) = group_clips(legs, 1)
    assert [leg.mode for leg in clip.legs] == ["ride", "train"]
    assert clip.mode == "train"  # the most real time


# ── pacing ───────────────────────────────────────────────────────────────────

def _distinct_day_legs(real_seconds, mode="ride"):
    base = date(2026, 5, 1)
    return [_leg(i, mode=mode, real_s=r, day=base + timedelta(days=i))
            for i, r in enumerate(real_seconds)]


@pytest.mark.parametrize("total", [30, 60, 90])
def test_durations_sum_to_total_and_respect_floor_and_cap(total):
    real = [60, 600, 3600, 36_000, 360_000, 10, 7200, 90_000, 5, 500] * 3
    legs = _distinct_day_legs(real)
    tl = build_timeline(LegSet(tuple(legs), ()), total)
    durations = [c.duration_s for c in tl.clips]
    assert tl.title_s + sum(durations) + tl.end_s == pytest.approx(total, abs=1e-9)
    assert tl.clips[0].start_s == TITLE_S
    assert tl.clips[-1].end_s == pytest.approx(total - END_S)
    cap = max(0.25, 1 / len(durations)) * clip_budget_s(total)
    assert all(MIN_CLIP_S - 1e-9 <= d <= cap + 1e-9 for d in durations)
    assert len(durations) <= max_clips(total)


def test_allocation_is_monotone_in_real_duration():
    real = [5, 60, 400, 3600, 20_000, 90_000, 400_000, 2_000_000, 10]
    clips = [Clip((leg,)) for leg in _distinct_day_legs(real)]
    times = SqrtBudgetPolicy().allocate(clips, clip_budget_s(60))
    by_real = [t for _, t in sorted(zip(real, times))]
    assert by_real == sorted(by_real)
    budget = clip_budget_s(60)
    assert max(times) == pytest.approx(0.25 * budget)  # the huge ones are capped
    assert min(times) == pytest.approx(MIN_CLIP_S)       # the tiny ones floored
    assert sum(times) == pytest.approx(budget)


def test_flight_weighs_half():
    # Five clips, so neither the floor nor the cap binds.
    modes = ("ride", "ride", "flight", "ride", "ride")
    legs = [_leg(i, mode=m, real_s=3600, day=date(2026, 5, 1) + timedelta(days=i))
            for i, m in enumerate(modes)]
    times = SqrtBudgetPolicy().allocate([Clip((l,)) for l in legs], clip_budget_s(60))
    assert times[2] == pytest.approx(times[0] / 2)


def test_zero_real_time_clips_still_fill_the_budget():
    legs = _distinct_day_legs([0.0, 0.0, 3600.0])
    times = SqrtBudgetPolicy().allocate([Clip((l,)) for l in legs], clip_budget_s(30))
    assert sum(times) == pytest.approx(clip_budget_s(30))
    assert min(times) >= MIN_CLIP_S - 1e-9


# ── the 365-day trip ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def big_legs():
    legset = build_legs(_big_trip())
    assert len(legset.legs) == 1000 and not legset.skipped
    return legset


def _signature(tl):
    return ([(c.start_s, c.end_s, tuple(l.leg.index for l in c.subs)) for c in tl.clips],
            [tl.sample(i / 30) for i in range(0, int(tl.total_s * 30) + 1, 7)])


@pytest.mark.parametrize("total", [30, 60, 90])
def test_long_trip_fits_every_length(big_legs, total):
    tl = build_timeline(big_legs, total)
    assert 1 <= len(tl.clips) <= max_clips(total)
    assert sum(len(c.subs) for c in tl.clips) == 1000
    durations = [c.duration_s for c in tl.clips]
    assert tl.title_s + sum(durations) + tl.end_s == pytest.approx(total)
    assert min(durations) >= MIN_CLIP_S - 1e-9
    assert _signature(tl) == _signature(build_timeline(big_legs, total))


def test_long_trip_allocation_and_samples_are_fast(big_legs):
    started = time.perf_counter()
    tl = build_timeline(big_legs, 60)
    for n in range(1800):
        tl.sample(n / 30)
    assert time.perf_counter() - started < 1.0


def test_counters_end_at_per_mode_totals(big_legs):
    tl = build_timeline(big_legs, 60)
    totals = {}
    for leg in big_legs.legs:
        totals[leg.mode] = totals.get(leg.mode, 0.0) + leg.km
    assert set(totals) == set(_MODES)
    end = tl.sample(tl.total_s)
    assert end.kind == "end"
    assert end.km_by_mode == pytest.approx(totals)
    # Just before the end card the last clip has drawn everything too.
    last = tl.sample(tl.clips_end_s - 1e-9)
    assert last.kind == "clip"
    assert last.km_by_mode == pytest.approx(totals, rel=1e-6)
    # And counters never go backwards.
    prev = {}
    for n in range(0, 1801):
        km = tl.sample(n / 30).km_by_mode
        for mode, value in prev.items():
            assert km.get(mode, 0.0) >= value - 1e-9
        prev = dict(km)


# ── sampling ─────────────────────────────────────────────────────────────────

def _small_timeline(total=30):
    d = date(2026, 5, 1)
    legs = [_leg(0, mode="ride", real_s=7200, day=d, lon0=0.0),
            _leg(1, mode="train", real_s=3600, day=d + timedelta(days=1), lon0=1.0),
            _leg(2, mode="hike", real_s=1800, day=d + timedelta(days=2), lon0=2.0)]
    return build_timeline(LegSet(tuple(legs), ()), total, start_date=d)


def test_sampling_at_clip_boundaries():
    tl = _small_timeline()
    assert tl.sample(0).kind == "title"
    assert tl.sample(TITLE_S - 1e-9).kind == "title"
    for i, clip in enumerate(tl.clips):
        first = clip.subs[0].leg
        at = tl.sample(clip.start_s)
        assert (at.kind, at.clip_index, at.sub_index) == ("clip", i, 0)
        assert (at.lon, at.lat) == pytest.approx(first.coords[0])
        assert at.km_by_mode.get(first.mode, 0.0) == pytest.approx(
            sum(l.km for l in tl.legs[:first.index] if l.mode == first.mode))
        if i:
            before = tl.sample(clip.start_s - 1e-9)
            prev_last = tl.clips[i - 1].subs[-1].leg
            assert before.clip_index == i - 1
            assert (before.lon, before.lat) == pytest.approx(prev_last.coords[-1], abs=1e-6)
    end = tl.sample(tl.clips_end_s)
    assert end.kind == "end"
    assert (end.lon, end.lat) == pytest.approx(tl.legs[-1].coords[-1])
    assert tl.sample(tl.total_s + 5).kind == "end"
    assert tl.sample(-1).kind == "title"


def test_frame_state_fields():
    tl = _small_timeline()
    clip = tl.clips[1]
    mid = tl.sample((clip.start_s + clip.end_s) / 2)
    assert mid.mode == "train" and mid.speed_kmh == 20.0
    assert mid.date == date(2026, 5, 2) and mid.day == 2
    assert mid.date_label == "Day 2 · 2 May 2026"
    assert 0 <= mid.heading < 360
    # Heading of an eastward-then-south-east leg piece is roughly east.
    assert 45 < mid.heading < 135


def test_ease_clock_is_monotone_and_starts_slow():
    d = 4.0
    xs = [ease_clock(d * i / 400, d) for i in range(401)]
    assert xs[0] == 0.0 and xs[-1] == pytest.approx(d)
    assert all(b >= a for a, b in zip(xs, xs[1:]))
    # Eased: the first hundredth of a second covers far less than linear.
    assert ease_clock(0.01, d) < 0.01 / 5
    # A short clip eases over a fifth of it, not 0.3 s.
    assert ease_clock(1.5, 1.5) == pytest.approx(1.5)


def test_short_sub_leg_is_drawn_at_once():
    d = date(2026, 5, 1)
    legs = [_leg(0, mode="ride", real_s=1_000_000, day=d),
            _leg(1, mode="ride", real_s=0.001, day=d)]
    tl = build_timeline(LegSet(tuple(legs), ()), 30)
    (clip,) = tl.clips
    tiny = clip.subs[1]
    assert tiny.instant
    at = tl.sample(tl.clips_end_s - 1e-6)
    assert at.sub_index == 1
    assert (at.lon, at.lat) == pytest.approx(legs[1].coords[-1])


def test_nothing_to_animate_and_too_short():
    with pytest.raises(NothingToAnimate):
        timeline_for_project(_project(_activity(1, start=None, end=None)), 60)
    with pytest.raises(ValueError):
        build_timeline(LegSet((_leg(0),), ()), 6)


def test_trip_start_sets_day_one():
    p = _project(_activity(1, day=date(2026, 5, 10)), trip_start="2026-05-01")
    tl = timeline_for_project(p, 30)
    assert tl.sample(TITLE_S + 0.5).day == 10


def test_cli_format_lists_clips_and_merged_legs():
    text = format_timeline(_small_timeline())
    assert "3 clips (max 16) from 3 legs" in text
    assert text.count("clip ") == 3
    assert "totals: ride" in text


def test_cli_prints_a_stored_trip(monkeypatch, capsys):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    import models.db as db_module
    from models.project_db import DBActivity, DBProject, DBProjectItem
    from models.user import UserInfo
    from src.video.__main__ import main

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        owner = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(owner)
        sess.commit()
        sess.refresh(owner)
        project = DBProject(user_info_id=owner.id, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        for pos, (aid, day) in enumerate(((11, "2026-06-01"), (12, "2026-06-01"),
                                          (13, "2026-06-02"))):
            sess.add(DBActivity(
                id=aid, user_info_id=owner.id, name=f"Walk {aid}", type="Walk",
                start_date=f"{day}T08:00:00Z", start_date_local=f"{day}T10:00:00Z",
                distance=5000.0, moving_time=3600,
                summary_polyline=polyline_lib.encode([(45.0, 6.0 + pos), (45.1, 6.1 + pos)])))
            sess.add(DBProjectItem(project_id=project.id, position=pos,
                                   item_type="activity", activity_id=aid))
        sess.commit()
        uid = owner.id

    assert main(["--project", "Trip", "--owner", str(uid), "--length", "30"]) == 0
    out = capsys.readouterr().out
    # The two walks of 1 June merge into one clip.
    assert "2 clips (max 16) from 3 legs" in out
    assert "Walk 11" in out and "Walk 13" in out
    assert main(["--project", "Nope", "--owner", str(uid)]) == 1


def test_pure_modules_import_no_framework():
    """Convention 4: legs, pacing and timeline pull in no Pillow, ffmpeg, DB
    or FastAPI."""
    code = ("import sys, src.video.legs, src.video.pacing, src.video.timeline\n"
            "bad = [m for m in ('fastapi', 'PIL', 'sqlalchemy', 'sqlmodel', 'models.db')"
            " if m in sys.modules]\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == ""


# ── the antimeridian (F1-5) ──────────────────────────────────────────────────

TOKYO = (35.55, 139.78)      # (lat, lon)
LAX = (33.94, -118.41)


def _pacific_trip() -> Project:
    """Tokyo, a flight to Los Angeles across the ±180° meridian, a walk there."""
    walk = [(LAX[0] + 0.001 * i, LAX[1] + 0.002 * i) for i in range(12)]
    return _project(
        _segment("flight", day=date(2026, 5, 1), start=TOKYO, end=LAX),
        _activity(1, type="Walk", day=date(2026, 5, 2), distance=2_500.0,
                  moving_time=1800, polyline=polyline_lib.encode(walk),
                  start=walk[0], end=walk[-1]))


def _wrapped(lon):
    return (lon + 180.0) % 360.0 - 180.0


def test_a_pacific_flight_is_animated_the_short_way():
    """Sampled every frame, the marker moves a little at a time and never
    over Europe or Africa, heading east the whole way."""
    tl = timeline_for_project(_pacific_trip(), 30)
    fps = tl.fps
    states = [tl.sample(n / fps) for n in range(int(tl.total_s * fps) + 1)]
    lons = [s.lon for s in states]
    assert max(abs(b - a) for a, b in zip(lons, lons[1:])) < 2.0
    assert min(abs(_wrapped(lon)) for lon in lons) > 110
    flight = [s for s in states if s.kind == "clip" and s.mode == "flight"]
    assert len(flight) > fps
    assert all(30 < s.heading < 150 for s in flight)


def test_longitudes_stay_continuous_across_legs():
    """The flight lands past +180 and the walk that follows starts there
    too, not 360° away; distances are those of the real route."""
    legs = build_legs(_pacific_trip()).legs
    flight, walk = legs
    lons = [lon for leg in legs for lon, _ in leg.coords]
    assert all(abs(b - a) < 180 for a, b in zip(lons, lons[1:]))
    assert flight.coords[0][0] == pytest.approx(TOKYO[1])
    assert flight.coords[-1][0] == pytest.approx(LAX[1] + 360)
    assert walk.coords[0][0] == pytest.approx(LAX[1] + 360, abs=1e-4)
    assert 8500 < flight.km < 9000
    assert walk.cum_km[-1] < 3.0
