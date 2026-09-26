"""Pure geometry helpers for editing an activity's track (issue #31).

Two responsibilities, both side-effect-free and standalone-testable:

1. **Canonical point list** — align ``summary_polyline`` (full-resolution track,
   every latlng re-encoded) with the parallel ``elevation_profile``
   (``distances_km`` / ``elevations_m``) into one ordered list of
   ``TrackPoint(lat, lng, elev)``.  Elevation is interpolated onto the polyline
   points by cumulative distance, since the two arrays are not guaranteed to be
   index-aligned.  ``points_to_polyline`` / ``points_to_elevation_profile``
   re-derive the storage arrays after an edit.

2. **Metric recomputation** — :func:`recompute_track_metrics` derives distance,
   elevation gain, hi/lo, start/end latlng, average speed, and *apportioned*
   moving/elapsed times for an edited (trimmed / split / point-edited) piece.
   Times are apportioned proportionally to retained distance because no
   per-point time stream is stored (only scalar times survive enrichment).
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from itertools import islice
from typing import List, Optional, Tuple

import polyline as polyline_lib

from src.models.great_circle import haversine_km
from src.models.value_bounds import (
    DISTANCE_MAX_M, DURATION_MAX_S, ELEVATION_MAX_M, ELEVATION_MIN_M, GAIN_MAX_M,
    finite_or_none, plausible_elevation,
)


@dataclass
class TrackPoint:
    lat: float
    lng: float
    elev: Optional[float] = None

    def __post_init__(self) -> None:
        # Every elevation the app stores or measures passes through here (GPX
        # upload, track editor, a profile read back): an implausible one is a
        # missing one (issue #462).
        if self.elev is not None:
            self.elev = plausible_elevation(self.elev)


def align_points(
    summary_polyline: Optional[str],
    elevation_profile: Optional[Tuple[List[float], List[float]]],
) -> List[TrackPoint]:
    """Align a polyline and an elevation profile into one ordered point list.

    Elevation values are interpolated onto the decoded polyline points by
    cumulative haversine distance.  When no elevation profile is present the
    points carry ``elev=None``.
    """
    if not summary_polyline:
        return []
    decoded = polyline_lib.decode(summary_polyline)  # [(lat, lng), …]
    if not decoded:
        return []

    dist_km: List[float] = []
    elev_m: List[float] = []
    if elevation_profile:
        dist_km, elev_m = elevation_profile[0] or [], elevation_profile[1] or []

    if not dist_km or not elev_m or len(dist_km) != len(elev_m):
        return [TrackPoint(lat=lat, lng=lng, elev=None) for lat, lng in decoded]

    # Cumulative distance (km) along the decoded polyline.
    cum: List[float] = [0.0]
    for i in range(1, len(decoded)):
        cum.append(cum[-1] + haversine_km(
            decoded[i - 1][0], decoded[i - 1][1], decoded[i][0], decoded[i][1]))

    points: List[TrackPoint] = []
    for (lat, lng), d in zip(decoded, cum):
        points.append(TrackPoint(lat=lat, lng=lng, elev=_interp_elev(d, dist_km, elev_m)))
    return points


def _interp_elev(d: float, dist_km: List[float], elev_m: List[float]) -> float:
    """Linearly interpolate elevation at cumulative distance *d* (km).

    Uses a binary search rather than a linear scan: called once per polyline
    point from align_points, a linear rescan-from-start made a full alignment
    O(N*M) (N polyline points, M elevation samples) — quadratic overall since
    N and M scale together, which measured at 20+ seconds for a ~40k-point
    activity (a few hours of dense GPS recording) and blew past the client's
    request timeout on save/split (issue #45).
    """
    if d <= dist_km[0]:
        return elev_m[0]
    if d >= dist_km[-1]:
        return elev_m[-1]
    i = bisect_left(dist_km, d)
    d0, d1 = dist_km[i - 1], dist_km[i]
    e0, e1 = elev_m[i - 1], elev_m[i]
    if d1 == d0:
        return e0
    frac = (d - d0) / (d1 - d0)
    return e0 + frac * (e1 - e0)


def points_to_polyline(points: List[TrackPoint]) -> Optional[str]:
    """Re-encode an ordered point list to a Google-encoded polyline string."""
    if not points:
        return None
    return polyline_lib.encode([(p.lat, p.lng) for p in points])


def points_to_elevation_profile(
    points: List[TrackPoint],
) -> Optional[Tuple[List[float], List[float]]]:
    """Re-derive ``(distances_km, elevations_m)`` from an ordered point list.

    Points that carry no elevation are INTERPOLATED across, linearly by
    cumulative distance between the bracketing known samples; a gap at the very
    start or end extends the nearest known value. Until issue #374 they were
    stored as ``0.0``, which is a fabricated reading, not a missing one: a track
    through the Alps with three ``<ele>``-less points was stored as diving to
    sea level and climbing back out. Anything recomputing from that series read
    the dive as real — a 300-point track at ~500 m measured 12.1 m of gain on
    the live import path and 152.3 m recomputed from its own stored profile.
    The chart, which plots this array verbatim, drew the dive too.

    Interpolating rather than carrying ``None`` through the profile, because:

    * :func:`align_points` already interpolates by cumulative distance when it
      reads a profile back, so the value stored here is the value every reader
      would have derived anyway — the gap is filled once, consistently, instead
      of differently by each reader;
    * the array stays numeric, which is what the chart and the low-res
      downsample (``downsample_elevation``) require — carrying ``None`` means
      teaching every reader, client included, to skip holes;
    * it makes a recompute from storage agree with the live path.
      :func:`recompute_track_metrics` drops elevation-less points while still
      accumulating their distance, so it sees a straight run between the
      bracketing samples — exactly what the interpolated series holds.

    Returns ``None`` when no point carries an elevation value (so the caller
    stores no elevation profile rather than a degenerate all-None one).
    """
    if not points or all(p.elev is None for p in points):
        return None
    distances: List[float] = [0.0]
    for i in range(1, len(points)):
        distances.append(distances[-1] + haversine_km(
            points[i - 1].lat, points[i - 1].lng, points[i].lat, points[i].lng))
    return distances, interpolate_elevation_gaps(
        distances, [p.elev for p in points])


def interpolate_elevation_gaps(
    distances_km: List[float], elevations: List[Optional[float]],
) -> List[float]:
    """Fill the ``None`` holes in an elevation series, by cumulative distance.

    Interior gaps are a straight line between the bracketing known samples;
    a leading or trailing gap extends the nearest known value, there being
    nothing on the other side to aim at. Every element must be ``None`` or a
    number and at least one must be a number.

    Shared with the repair migration for issue #374, which maps the stored
    ``0.0`` sentinel back to ``None`` and calls this — so already-stored rows
    are repaired to exactly what the fixed writer would have produced.
    """
    known = [i for i, e in enumerate(elevations) if e is not None]
    if not known:
        raise ValueError("no point carries an elevation")
    filled: List[float] = [
        e if e is not None else 0.0 for e in elevations]  # holes overwritten below
    for i in range(known[0]):
        filled[i] = filled[known[0]]
    for i in range(known[-1] + 1, len(filled)):
        filled[i] = filled[known[-1]]
    for a, b in zip(known, known[1:]):
        if b == a + 1:
            continue
        d0, d1 = distances_km[a], distances_km[b]
        e0, e1 = filled[a], filled[b]
        span = d1 - d0
        for i in range(a + 1, b):
            frac = (distances_km[i] - d0) / span if span else 0.0
            filled[i] = e0 + frac * (e1 - e0)
    return filled


def clean_elevation_profile(
    distances_km: List, elevations: List,
) -> Optional[Tuple[List[float], List[float]]]:
    """A stored-form ``(distances_km, elevations_m)`` from raw samples, with
    no value that is not a finite number (issue #462).

    A sample whose distance is not a finite number has no place on the
    profile and is left out. An elevation that is not a plausible reading
    (NaN, ±Infinity, null, past ±20 km: see
    :func:`~src.models.value_bounds.plausible_elevation`) is a missing one,
    filled by :func:`interpolate_elevation_gaps`
    as :func:`points_to_elevation_profile` fills a point without ``<ele>``.
    Returns ``None`` when fewer than two samples or no elevation remain: no
    profile, rather than a degenerate one.

    Shared with the repair migration for issue #462, so already-stored rows
    are repaired to exactly what the writers now store.
    """
    dists: List[float] = []
    elevs: List[Optional[float]] = []
    for d, e in zip(distances_km, elevations):
        d = finite_or_none(d)
        if d is None:
            continue
        dists.append(d)
        elevs.append(plausible_elevation(e))
    if len(dists) < 2 or all(e is None for e in elevs):
        return None
    return dists, interpolate_elevation_gaps(dists, elevs)


def elevation_profile_from_streams(
    distance_m: List, altitude_m: List,
) -> Optional[Tuple[List[float], List[float]]]:
    """An activity's elevation profile from its Strava ``distance`` and
    ``altitude`` streams (metres), cleaned by :func:`clean_elevation_profile`.
    Samples past the shorter stream are dropped."""
    n = min(len(distance_m), len(altitude_m))
    distances_km = [
        d / 1000 if finite_or_none(d) is not None else None for d in distance_m[:n]]
    return clean_elevation_profile(distances_km, altitude_m[:n])



def _reading(value) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _all_within(values, lo: float, hi: float) -> bool:
    """True if *values* are all finite numbers within [lo, hi]. Checked in C
    for the common case, a clean list of floats, since a profile can hold
    millions: a finite sum means no NaN or infinity, and then min and max
    can be trusted. Anything else is judged value by value."""
    if set(map(type, values)) <= {int, float}:
        total = sum(values)    # an int when all are: finite, and never converted
        if isinstance(total, int) or math.isfinite(total):
            return not values or (lo <= min(values) and max(values) <= hi)
    return all(finite_or_none(v) is not None and lo <= v <= hi for v in values)


def profile_needs_repair(profile) -> bool:
    """True if a stored ``(distances_km, elevations_m)`` holds a sample the
    writers no longer store: a distance that is not a finite number, or an
    elevation that is not a plausible reading (NaN, ±Infinity, null, past
    ±20 km). Such profiles were written before issue #462."""
    dists, elevs = profile
    return not (_all_within(dists, -math.inf, math.inf)
                and _all_within(elevs, ELEVATION_MIN_M, ELEVATION_MAX_M))


def repair_elevations(profile, gain, high, low, *, app_measured: bool):
    """An activity's elevation figures as the writers now store them.

    Before issue #462 an activity could store a non-finite or implausible
    elevation (a GPX ``<ele>NaN</ele>``, ``inf``, or a device's ``65535``) in
    its profile, its high/low and, measured from them, its gain. This returns
    ``(profile, gain, high, low)`` repaired to what the writers would store
    from the same readings; the trip-file import and the repair migration
    6c1f0e9a2b47 both go through it, so an old export and an old row come out
    the same. The profile returned is the one given unless it was repaired.

    * A profile needing repair is rebuilt by :func:`clean_elevation_profile`.
      One holding anything but numbers and nulls is not a profile any writer
      made, and is returned as it is, for the caller to refuse.
    * *app_measured* (an uploaded GPX track): the app measured gain, high and
      low from those readings, so once the profile is repaired they are
      measured again from it. Anyone else's (Strava's, or its share of them
      on an edited activity, #386) are kept.
    * Any figure still not plausible is measured from the profile, or else
      cleared: gain to 0.0, high and low to None. A None stays None.
    """
    repaired = False
    if (profile is not None and profile_needs_repair(profile)
            and all(map(_reading, profile[0])) and all(map(_reading, profile[1]))):
        profile = clean_elevation_profile(*profile)
        repaired = True

    def measured():
        if profile is None or profile_needs_repair(profile):
            return 0.0, None, None
        dists, elevs = profile
        return float(elevation_gain(elevs, dists)), max(elevs), min(elevs)

    if repaired and app_measured:
        new_gain, high, low = measured()
        gain = new_gain if gain is not None else None
    bad_gain = gain is not None and not (
        finite_or_none(gain) is not None and 0 <= gain <= GAIN_MAX_M)
    bad_high = high is not None and plausible_elevation(high) is None
    bad_low = low is not None and plausible_elevation(low) is None
    if bad_gain or bad_high or bad_low:
        m_gain, m_high, m_low = measured()
        gain = m_gain if bad_gain else gain
        high = m_high if bad_high else high
        low = m_low if bad_low else low
    return profile, gain, high, low


def repair_elapsed(elapsed, moving):
    """An activity's elapsed time as the writers now store it.

    Before issue #462 a GPX upload took its span from the file's earliest and
    latest stamps, so one stray stamp (a clock that read 1970) made it
    decades long. Elapsed time past :data:`DURATION_MAX_S` is no measurement:
    it becomes the moving time, which never counted such a gap (#462), or 0.
    """
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and (
            finite_or_none(elapsed) is None or elapsed > DURATION_MAX_S):
        plausible = (isinstance(moving, (int, float)) and not isinstance(moving, bool)
                     and finite_or_none(moving) is not None and 0 <= moving <= DURATION_MAX_S)
        return int(moving) if plausible else 0
    return elapsed


#: Distance, in metres of travel, spanned by the centred moving average applied
#: to elevations before gain is accumulated. Measured in DISTANCE, not samples:
#: a fixed sample count means a window whose real width depends entirely on the
#: recording rate. An 11-sample window is ~60 m of a 1 Hz ride and a full
#: kilometre of a route exported at one point per 100 m, which erased every hill
#: on planned-route GPX — reporting 3 m of climb on a route with 600 m of it.
#:
#: This is the FLOOR, not the width: a series noisy enough to need more gets a
#: wider window (see :func:`_smooth_elevations`), because 60 m of a track
#: sampled every 40 m holds a single point and averages nothing at all.
ELEV_SMOOTH_SPAN_M = 60.0

#: Ceiling on that widening. Past a few hundred metres a moving average stops
#: telling noise from terrain — a 240 m window already halves a 500 m roller —
#: so beyond this we stop filtering and let the hysteresis band absorb whatever
#: noise is left.
#:
#: Fixture, spelled out so the number can be re-derived: 30 sinusoidal hills of
#: 20 m over 600 m (12 km of track) sampled every 100 m under sigma 2 noise,
#: true ascent 600 m, mean of 20 noise seeds. This cap recovers 239 m
#: (218-254); letting the window widen without limit recovers 108 m (4-252) —
#: it swallows the hills outright on the noisier seeds.
ELEV_SMOOTH_MAX_SPAN_M = 240.0

#: Residual noise the window aims to leave behind, in metres. Averaging m
#: samples divides noise by sqrt(m), so this fixes how many samples the window
#: must hold: ``(sigma / target) ** 2``. Lower means more filtering and flatter
#: terrain, so this is the largest value that still clears the bar everywhere.
#: Over 20 noise seeds, 1.25 left 51 m of phantom climb on held-altitude flat
#: ground and 1.5 left 64 m on sparse flat ground; 1.0 caps both at 31 m. Going
#: below buys nothing — hill recovery is already identical at 1.25.
ELEV_SMOOTH_TARGET_SIGMA_M = 1.0

#: Multiple of the RESIDUAL noise — what survives smoothing, not what went in —
#: used as the hysteresis band, and the floor/ceiling it is clamped to. Sizing
#: it from the raw noise double-counts: smoothing already removed most of that,
#: and a band wide enough for unfiltered noise eats real rollers (20 m hills on
#: a sparse noisy track read 236 m of a true 600 that way, 349 m this way). A
#: smoothed series wanders rather than jitters, so the band has to cover the
#: largest excursion of a correlated series, not a one-sample outlier — several
#: sigma, not the 3x that suits per-sample jitter.
#:
#: Fixture: 30 km of flat ground sampled every 40 m under sigma 3 noise, true
#: ascent 0, mean (and worst) of 20 noise seeds. 3x leaves 25 m of phantom
#: climb (worst 52), 4x leaves 8 m (worst 35), this 4.5x leaves 3 m (worst 28),
#: 5x leaves 0 m (worst 7). The last step is not free: a wider band also eats
#: real rollers, and 4.5 is where flat ground is quiet without spending more
#: hill than it has to.
ELEV_NOISE_SIGMA_MULTIPLE = 4.5
ELEV_GAIN_THRESHOLD_MIN_M = 1.0
ELEV_GAIN_THRESHOLD_MAX_M = 20.0

#: Difference orders the noise estimate is taken over. See
#: :func:`_noise_estimate` for why the run starts at 3, and why the SMALLEST
#: estimate across it is the answer. Below this many differences a median of
#: them means nothing and the estimate is abandoned.
_NOISE_DIFFERENCE_ORDERS = range(3, 6)
_NOISE_MIN_DIFFERENCES = 8

#: Independent per-sample noise of sigma appears in a series' k-th differences
#: at ``sigma * sqrt(C(2k, k))`` — sqrt(20) for third differences, sqrt(70) for
#: fourth, sqrt(252) for fifth. Dividing it back out recovers sigma; the 1.4826
#: turns a median absolute deviation into a Gaussian standard deviation.
_NOISE_DIFFERENCE_SCALE = {
    k: 1.4826 / math.sqrt(math.comb(2 * k, k)) for k in _NOISE_DIFFERENCE_ORDERS
}

#: Most consecutive repeats treated as one altitude reading. Devices that update
#: altitude less often than position emit runs of identical values, and
#: differencing INSIDE such a run yields zero, hiding the noise completely.
_NOISE_MAX_RUN_STRIDE = 16

#: How many differences are actually sorted to take a median. A median over this
#: many is accurate to well under a percent, and sorting all 200k of a long ride
#: three times over cost 0.27 s of the 0.68 s a full pass took.
_NOISE_MEDIAN_SAMPLE_CAP = 20_000


def _run_stride(elevations: List[float]) -> int:
    """Samples per distinct altitude reading, as a median run length.

    A phone whose barometer updates at 0.2 Hz while position updates at 1 Hz
    writes each altitude five times over. Every difference taken inside such a
    run is exactly zero, so a median over differences collapses to zero and the
    series reads as noise-free — while the steps BETWEEN readings carry the full
    noise. Measured on 30 km of flat ground with altitude held for five samples,
    the band fell to its 1 m floor and reported 155 m of climb at sigma 1.2 and
    634 m at sigma 3. Differencing at this stride lands on separate readings.

    The median rather than the mean, so one long stationary stretch cannot
    stretch the stride for a whole track, and capped at
    :data:`_NOISE_MAX_RUN_STRIDE`. Ordinary quantisation — barometric altitude
    rounded to 0.1, 0.2 or 1 m — produces runs of one or two samples and so
    leaves the stride at 1, which is what keeps it out of this path.
    """
    counts = [0] * (_NOISE_MAX_RUN_STRIDE + 1)
    runs = 0
    length = 1
    for previous, current in zip(elevations, elevations[1:]):
        if previous == current:
            length += 1
        else:
            counts[length if length < _NOISE_MAX_RUN_STRIDE
                   else _NOISE_MAX_RUN_STRIDE] += 1
            runs += 1
            length = 1
    counts[length if length < _NOISE_MAX_RUN_STRIDE
           else _NOISE_MAX_RUN_STRIDE] += 1
    runs += 1

    seen = 0
    for stride in range(1, _NOISE_MAX_RUN_STRIDE + 1):
        seen += counts[stride]
        if seen * 2 > runs:
            return stride
    return _NOISE_MAX_RUN_STRIDE


def _noise_estimate(elevations: List[float]) -> Tuple[float, int]:
    """Per-sample sensor noise in metres, plus the run stride it was read at.

    Noise cannot be inferred from how the track was sampled, which is what
    issue #376 is about. Spacing says nothing about the source: a non-barometric
    phone, a Garmin on smart recording and a track simplified before upload are
    all sparse AND noisy, while a route planner's DEM export is sparse and
    clean. So measure it instead of guessing from geometry.

    The measurement is a median absolute k-th difference scaled back to a
    standard deviation. Differencing a series k times annihilates any local
    polynomial of degree below k, so terrain must bend more sharply than a
    degree-k polynomial to register at all, while independent noise — having no
    shape to annihilate — always survives at ``sigma * sqrt(C(2k, k))``.

    Two properties follow, and both were premises the previous estimator got
    wrong:

    * **Curvature.** Second differences (what it used) are near zero only on a
      STRAIGHT flank; they scale with the square of the spacing on any bend, so
      a clean curved route at 100 m spacing read as sigma 2 and earned a 5 m
      band that erased its rollers whole. Higher differences vanish on a bend
      too: clean sinusoidal rollers of 10 m over 600 m at 100 m spacing measure
      sigma 0.23 here, against 0.83 from second differences.
    * **Scale.** Whatever terrain still shows through shrinks with every added
      order — for anything longer than ~3.4 samples per cycle — while the noise
      estimate is unchanged by construction. So take the SMALLEST estimate over
      a run of orders: the one least contaminated by terrain. Orders 3 to 5;
      order 2 is the discredited one, and by order 6 the minimum of six noisy
      estimates starts biasing sigma low (a true 1.2 measured 1.08, enough to
      let 32 m of phantom climb back into held-altitude flat ground).

    Differences are taken at the run stride so repeated altitude readings are
    not differenced against themselves — see :func:`_run_stride`.

    Returns ``0.0`` for a series too short to have
    :data:`_NOISE_MIN_DIFFERENCES` third differences. The band then sits at its floor, which keeps the genuine
    steps in a handful of points — the tail of an edited piece — rather than
    dismissing them as jitter on the strength of two or three samples.
    """
    stride = _run_stride(elevations)
    differences = elevations
    estimate: Optional[float] = None
    for order in range(1, _NOISE_DIFFERENCE_ORDERS.stop):
        differences = [b - a for a, b
                       in zip(differences, islice(differences, stride, None))]
        if len(differences) < _NOISE_MIN_DIFFERENCES:
            break
        if order not in _NOISE_DIFFERENCE_ORDERS:
            continue
        step = 1 + len(differences) // _NOISE_MEDIAN_SAMPLE_CAP
        magnitudes = sorted(map(abs, differences[::step]))
        middle = magnitudes[len(magnitudes) // 2]
        sigma = _NOISE_DIFFERENCE_SCALE[order] * middle
        estimate = sigma if estimate is None else min(estimate, sigma)
    return (estimate if estimate is not None else 0.0), stride


def _smooth_elevations(
    elevations: List[float],
    distances_km: Optional[List[float]],
    sigma: float,
    stride: int,
) -> Tuple[List[float], float]:
    """Centred moving average over travel, widened to suit the measured noise.

    The span is measured in distance, not samples, so the same terrain recorded
    at 1 Hz and at one point per 100 m gets the same physical filtering. A fixed
    span is not enough on its own, though: :data:`ELEV_SMOOTH_SPAN_M` of a track
    sampled every 40 m holds one point and averages nothing, which is why the
    previous version skipped sparse series outright — on the theory that sparse
    meant route-planner DEM output with no noise in it. It does not. 30 km of
    flat ground at 40 m spacing under sigma 3 came back as 652 m of climb.

    So the span grows with the noise instead. Averaging m samples divides noise
    by sqrt(m) and we want :data:`ELEV_SMOOTH_TARGET_SIGMA_M` left, so the
    window must hold ``(sigma / target) ** 2`` readings — times the run stride,
    since repeated readings average to themselves and count for one. A clean
    series asks for none of this and keeps the 60 m floor, which is what leaves
    a sparse planned route untouched. Capped at
    :data:`ELEV_SMOOTH_MAX_SPAN_M`, beyond which the band takes over.

    The window is clamped at both ends rather than padded, so the first and last
    samples average over whatever neighbours exist. Endpoints therefore keep
    slightly more of their own noise than the middle does — immaterial for a
    gain figure accumulated over the whole track.

    Returns the smoothed series and the mean number of samples its windows
    held, which is how :func:`_noise_threshold` knows what noise is left. The
    mean, not the median, purely because it needs no second pass over 200k
    window widths — the two differ only by the half-width windows at the ends.
    Without ``distances_km`` (a caller that has elevations only) the series is
    returned untouched with a window of 1: a guessed span is worse than none,
    and the band then sizes itself from the full noise.
    """
    n = len(elevations)
    if n < 3 or not distances_km or len(distances_km) != n:
        return list(elevations), 1.0

    gaps = sorted([b - a for a, b
                   in zip(distances_km, islice(distances_km, 1, None))])
    median_gap_m = gaps[len(gaps) // 2] * 1000.0
    wanted = stride * (sigma / ELEV_SMOOTH_TARGET_SIGMA_M) ** 2 * median_gap_m
    span = max(ELEV_SMOOTH_SPAN_M, min(ELEV_SMOOTH_MAX_SPAN_M, wanted))
    half_km = (span / 2.0) / 1000.0

    # Prefix sums so each window costs one subtraction rather than re-adding its
    # members: the window holds ~11 samples of a 1 Hz ride but ~60 of a track
    # recorded every metre, and re-summing made the whole pass O(n * window).
    prefix: List[float] = [0.0]
    for value in elevations:
        prefix.append(prefix[-1] + value)

    out: List[float] = []
    total_width = 0
    lo = hi = 0
    for i in range(n):
        while distances_km[i] - distances_km[lo] > half_km:
            lo += 1
        while hi + 1 < n and distances_km[hi + 1] - distances_km[i] <= half_km:
            hi += 1
        width = hi + 1 - lo
        total_width += width
        out.append((prefix[hi + 1] - prefix[lo]) / width)
    return out, total_width / n


def _noise_threshold(sigma: float, stride: int, window: float) -> float:
    """Hysteresis band, sized from the noise smoothing did NOT remove.

    ``window`` samples averaged is ``window / stride`` independent readings
    averaged, which leaves ``sigma / sqrt(window / stride)``. Sizing the band
    from the raw sigma instead charges twice for noise already filtered out, and
    the band is paid for in real terrain: it costs roughly its own height per
    roller.

    Clamped: never below :data:`ELEV_GAIN_THRESHOLD_MIN_M`, so a perfectly clean
    series still ignores floating-point dust, and never above
    :data:`ELEV_GAIN_THRESHOLD_MAX_M`, so a catastrophically noisy one cannot
    raise the band until real mountains fit inside it. That ceiling is only
    reached past ~5 m of residual noise, by which point no hill it could swallow
    was resolvable anyway.
    """
    residual = sigma / math.sqrt(max(1.0, window / stride))
    return min(ELEV_GAIN_THRESHOLD_MAX_M,
               max(ELEV_GAIN_THRESHOLD_MIN_M,
                   ELEV_NOISE_SIGMA_MULTIPLE * residual))


def elevation_gain(
    elevations: List[float],
    distances_km: Optional[List[float]] = None,
    *,
    noise: Optional[Tuple[float, int]] = None,
) -> float:
    """Total ascent in metres over an ordered elevation series.

    Summing every positive sample-to-sample delta — which is what this did until
    issue #260 — counts sensor noise as climbing. Every upward flicker is added
    and no downward one subtracts it, so the error accumulates in one direction
    and grows with the sample count. On a 6000-sample track with a true 600 m
    climb and ±1.2 m of ordinary GPS/barometric noise it reported 4094 m.

    So: measure the noise (:func:`_noise_estimate`), smooth over enough distance
    to bring it down (:func:`_smooth_elevations`), then accumulate through a
    hysteresis band sized from whatever is left (:func:`_noise_threshold`).
    ``ref`` is the last elevation counted; a rise is booked only once the series
    climbs a band above it, and ``ref`` only follows the series down once it
    falls a band below. Movement inside the band is drift. That fixture then
    reports 599.2 m.

    All three steps hang off one measurement, and that is what issue #376 fixed:
    the previous version inferred noise from sampling geometry instead, skipping
    smoothing on any sparse series and reading its band off second differences.
    Both premises were false. Spacing does not identify the source — a
    non-barometric phone is sparse AND noisy, and 30 km of flat ground at 40 m
    spacing with sigma 3 read 652 m of climb, now 0. And second differences are
    near zero only on straight flanks, not on bends — clean sinusoidal rollers
    of 10 m over 600 m at 100 m spacing read 0 of their true 500, now 500.

    ``distances_km`` is the cumulative distance of each sample, and is what
    makes the window physical rather than a function of the recording rate.

    Strava-synced activities never reach here — their ``total_elevation_gain``
    arrives already processed. This is the figure for tracks we derive
    ourselves: hand-edited pieces, splits, and GPX imports.

    ``noise`` overrides the measurement with a caller's own ``(sigma, stride)``.
    Only :func:`terrain_corrected_gain` passes it, and it has to: the series it
    hands over is part recording and part noise-free terrain model, so measuring
    that mixture reads the model's calm as the sensor's and leaves the
    recording's own stretches neither smoothed nor banded.
    """
    if len(elevations) < 2:
        return 0.0
    sigma, stride = noise if noise is not None else _noise_estimate(elevations)
    smoothed, window = _smooth_elevations(
        elevations, distances_km, sigma, stride)
    threshold = _noise_threshold(sigma, stride, window)
    gain = 0.0
    ref = smoothed[0]
    for value in smoothed[1:]:
        if value - ref >= threshold:
            gain += value - ref
            ref = value
        elif ref - value >= threshold:
            ref = value
    return gain


# ── Terrain-model gain: the flatness oracle (issue #386) ──────────────────────
#
# Everything above works on one series of elevations over distance, and that is
# why the phantom climb on a phone recording cannot be removed there. Correlated
# sensor drift and gentle terrain have the SAME SPECTRUM on the distance axis:
# they are not separable, which was established by sweeping every span x
# threshold pair, and by PR #389 measuring the marginal sigma correctly and
# erasing up to 100% of a real 1000 m day for it. The time axis does not rescue
# it either — at roughly constant speed time and distance are one axis up to a
# scale factor.
#
# So this does not try. It brings in a SECOND, INDEPENDENT measurement — the
# elevation a terrain model reports along the same path — and uses it for the one
# question it can answer without ambiguity: *is there any relief here at all?*
#
# Where the terrain model says there is none, every metre the sensor reported is
# invented, and the terrain model's own steps replace it. Where the terrain model
# sees relief, the recording stands untouched -- and the justification for that
# is "it changes nothing", not "the recording is better".
#
# Worth being exact about why, because the obvious story is wrong. On a 10 m/200 m
# roller field the recording reports 858 m against a true 999 and the model 704,
# so the recording does look better. But that is a coincidence of two errors
# pointing opposite ways, not information the recording has and the model lacks.
# Measured on the clean surface with no sensor at all: the 30 m GRID loses only 3%
# of that roller field (999 -> 967), while running this same gain pipeline over
# the PERFECT analytic surface loses 20% (999 -> 800), because its own 60 m
# smoothing floor cannot see a 200 m wave either. At 100 m wavelength the split is
# starker: the grid loses 17% (1998 -> 1660) and the pipeline 57% (1998 -> 850).
# The dominant term is this module's smoothing, not the model's resolution. So the
# recording's 858 is 800 of pipeline-limited terrain plus phantom climb that
# happens to back-fill what the smoothing removed.
#
# Which means substitution's ceiling is a limit of THIS CODE more than of the
# data, and it would move if the smoothing floor did. Keeping the recording where
# there is relief is defensible because it is a no-op, and that is the whole of
# the argument.

#: Length of path over which the relief question is asked, in metres.
#:
#: Per-activity is too coarse — a walk half along a flat promenade and half up a
#: hill has to keep its hill, and one verdict for the whole track cannot do that
#: (measured: 452 m reported against a true 300; windowed, 309).
TERRAIN_WINDOW_M = 500.0

#: How much relief a window must show before the recording is trusted, in metres.
#:
#: Measured per 500 m window over every window of all five benchmark seeds, so
#: these ranges are reproducible from the committed fixtures: flat ground reads
#: 0.00, a smoothed path wandering across a 20% slope 0.71-4.20 (pure artefact),
#: a shallow river valley 1.85-2.08, a 1.5% drag 6.95-7.78, and 10 m rollers
#: 8.95-9.98.
#:
#: The valley and the cross-slope artefact OVERLAP, so no threshold separates
#: them. 5 m puts both on the flat side, which is right for the artefact and, as
#: it happens, near-right for the valley too (38.6 m reported against a true 40)
#: because a terrain model is accurate on relief that shallow. Dropping to 2 m to
#: "save" the valley instead lets the artefact back in: 131-190 m across those
#: seeds, against 27-36 m at 5 m.
#:
#: The artefact's 4.20 is closer to this threshold than is comfortable. It is the
#: horizontal-into-vertical coupling term and it grows with the fix quality --
#: at 10 m of horizontal error the artefact alone reports about 114 m.
TERRAIN_RELIEF_M = 5.0



def terrain_corrected_gain(
    recorded: List[float],
    terrain: List[float],
    distances_km: Optional[List[float]] = None,
    *,
    window_m: float = TERRAIN_WINDOW_M,
    relief_m: float = TERRAIN_RELIEF_M,
) -> float:
    """Ascent over a recording, with flat stretches taken from the terrain model.

    ``terrain`` is the terrain-model elevation at each of ``recorded``'s points,
    sampled along a path already smoothed in plan view — smoothing the PATH is
    legitimate in a way smoothing the elevation is not, because a road's plan
    geometry is smooth at a scale far above GPS horizontal error. The two lists
    must be the same length; ``distances_km`` is as :func:`elevation_gain` takes
    it.

    Falls back to :func:`elevation_gain` on the recording alone when no terrain
    is available, which is a normal state and not an error: the tile source has
    no SLA, and an end-to-end encrypted activity's geometry cannot be read by
    the server at all.

    **Why the windows splice deltas rather than sum their own gains.** Summing a
    per-window :func:`elevation_gain` resets the hysteresis reference and
    re-smooths the series at every boundary, which read 766 m where the recording
    reads 858 m on 200 m rollers — a 92 m loss on exactly the case this is meant
    to leave alone. Composing the steps into one series and accumulating ONCE
    makes the all-relief case bit-identical to the recording, which is the
    property that makes this safe. Splicing steps rather than elevations also
    disposes of the offset between the two sources for free: barometric drift
    puts them at different levels, and only their steps are ever used.
    """
    if not terrain or len(terrain) != len(recorded):
        return elevation_gain(recorded, distances_km)
    if len(recorded) < 3:
        return elevation_gain(recorded, distances_km)

    series = [recorded[0]]
    for lo, hi in _terrain_windows(distances_km, len(recorded), window_m):
        # ``hi`` is one sample PAST the window's own end, so consecutive windows
        # share a boundary sample and every step in the track is contributed
        # exactly once. Ending at the window's last sample instead drops the
        # step across each boundary — 40 of them on a 20 km track at this window
        # size — which is enough on its own to stop the all-relief case being
        # the exact no-op the docstring above claims.
        verdict = terrain[
            _relief_span(lo, hi, len(recorded), window_m, distances_km)]
        flat = len(verdict) >= 3 and _relief(verdict) < relief_m
        source = terrain if flat else recorded
        for previous, current in zip(source[lo:hi], source[lo + 1:hi]):
            series.append(series[-1] + (current - previous))

    # The noise measurement comes from the RECORDING, never from the series
    # built above. That series is part recording and part terrain model, and the
    # model has no sensor error at all: on a track that is more than about half
    # flat the median k-th difference becomes a model sample, sigma collapses to
    # zero, and the smoothing span and hysteresis band both drop to their floors
    # — so the recording's noise in the windows that KEPT the recording is
    # neither smoothed nor banded. Measured on a 40 m-spacing track with 3 m of
    # white noise, half flat then a 300 m hill: 454-544 m reported against a
    # true 300, where the recording alone gives 287-297 and the model 300. The
    # mixture was worse than either source it was spliced from.
    return elevation_gain(series, distances_km, noise=_noise_estimate(recorded))


def _relief(window: List[float]) -> float:
    """How much the terrain model rises and falls across *window*, in metres.

    A plain range, and the plainness is a measured choice rather than the
    obvious one. Two alternatives were tried against the benchmark and both are
    worse:

    * **Trimming the extreme sample at each end** is wrong on monotonic ground,
      where the extremes ARE the signal: it read a 368 m stretch of a 1.5% drag
      as 4.999 m and called it flat.
    * **Smoothing the model first** was introduced to stop one bad post
      deciding a window, and it blinds the verdict at its own wavelength — 60 m
      of smoothing took a 10 m/100 m roller field from 8.05 m of relief to 3.81
      and substituted the model over real hills. The same trap as the 60 m
      elevation smoothing this whole issue is about.

    Against a PERFECT model the range separates the cases cleanly, and that is
    what the gates assert. Relief per 500 m window, over every window of all
    five benchmark seeds:

    ===============  ==============     ==========  ===========
    should read flat                    should read relief
    ------------------------------     -------------------------
    flat ground      0.00               1.5% drag   6.95 - 7.78
    cross-slope      0.71 - 4.20        rollers     8.95 - 9.98
    shallow valley   1.85 - 2.08
    ===============  ==============     ==========  ===========

    **Give the model its own error and the verdict stops being reliable.** Both
    verdicts degrade, on BOTH axes — the error's size and its correlation length
    — so this is one 2-D surface and not two stories. 40 seeds; sigma is what
    the TRACK sees along the path it reads, calibrated per row, because the
    bilinear read damps independent error by about a quarter and correlated
    error not at all (a sweep at fixed nominal sigma is otherwise partly an
    amplitude sweep, which is how an earlier version of this table came to
    understate the flat column by 3x):

    ==============  ==================  ====================
                    drag's no-op holds  flat windows reading
    correlation     (of 40 seeds)       relief wrongly
    --------------  ------------------  --------------------
    length          s0.5  s1.0  s1.5    s0.5   s1.0    s1.5
    ==============  ==================  ====================
    perfect model     40    40    40    0.0%   0.0%    0.0%
    independent       40    40    40    0.0%  25.0%   82.9%
    45 m              40    37    38    0.0%   6.1%   59.6%
    100 m             40     8     3    0.0%   1.2%   25.2%
    300 m             39     7     0    0.0%   0.1%    4.1%
    500 m             40    20     2    0.0%   0.0%    1.0%
    1500 m            40    40    37    0.0%   0.0%    0.0%
    ==============  ==================  ====================

    Read it as one surface:

    * **At sigma 0.5 everything holds** — 39-40 of 40 and 0.0% in every cell.
      **That is the tolerance this design has, and it is the number that
      matters.**
    * **By sigma 1.0 both verdicts are in trouble, at opposite ends.** The
      relief verdict collapses where the error correlates near the window — 8 of
      40 at 100 m, 7 at 300 m — because a correlated slope inside a window
      cancels the terrain's. The flat verdict fails at the short end instead,
      25% of windows misreading under independent error: a range cannot average
      excursions away, so the more a window holds the further apart its extremes.
    * Only error correlated over several times the window is benign both ways,
      because within one window that is a constant offset a range subtracts out.

    Neither axis "governs" a verdict: the relief column moves 40 -> 8 -> 3 with
    sigma alone at 100 m, and the flat column moves 82.9% -> 4.1% -> 0.0% with
    length alone at sigma 1.5. Earlier versions of this docstring told a tidier
    story three times running — a 0.2 m margin from one seed, then "correlated
    error mostly cancels", then "independent axes" — and each was a
    simplification the data did not support.

    So the number to measure is the **effective within-window error of a real
    tileset over known-flat ground**, against a tolerance of about 0.5 m. It
    cannot be inferred from the spec sheet, which is worth spelling out because
    the arithmetic looks like it works: Copernicus GLO-30 quotes a *relative*
    vertical accuracy of 2 m LE90, and 2 / 1.645 is 1.2. Two reasons not to
    believe that figure here — it is for slopes under 20% (4 m above, and the
    cross-slope case in this benchmark is a 20% slope), and "relative accuracy"
    is quoted over a baseline rather than between neighbouring posts, so most of
    it is structure larger than a post and the within-window component is
    smaller. 1.2 is an upper bound, and an upper bound is the pessimistic end.

    The window length is the obvious free parameter, and it is not a free fix:
    lengthening it to save the relief verdict (at 750 m the drag holds
    everywhere) enlarges what the "should read flat" cases show too, and the
    valley and cross-slope cross 5 m instead. Any change has to move the
    threshold with it, and both have to be re-measured against the table above.

    No statistic tried does better once model error is present: a trimmed range,
    p95-p5, IQR, 2.5 sigma, median-filtered and boxcar ranges at 30-120 m, and
    the summed absolute 30 m step were all measured and none separates the
    groups. Smoothing additionally blinds the verdict at its own wavelength —
    60 m of it took a 10 m/100 m roller field from 8.95 m of relief to 3.99 and
    substituted the model over real hills, the same trap as the 60 m elevation
    smoothing this issue is about. So the plain range ships as the simplest
    thing that is no worse.

    **This is why nothing here is wired to an activity yet.** Unit 2 has to
    measure a real tileset's error correlation at the 500 m scale against known
    terrain; if it sits near the window, this verdict needs rethinking rather
    than retuning, and the window length is the first thing to reconsider. The
    sensitivity is printed by ``python -m tests.elevation_bench`` across all
    five seeds rather than gated — gating a number nobody has measured on real
    tiles would only encode a guess, and quoting one seed of it is how the
    previous version of this docstring came to claim a 0.2 m margin that three
    seeds out of five do not have.
    """
    return max(window) - min(window)


def _terrain_windows(
    distances_km: Optional[List[float]], count: int, window_m: float
):
    """Walk the track in *window_m* steps of GROUND, yielding ``(lo, hi)``.

    ``hi`` is exclusive of the window but one past its last sample, so windows
    overlap by one and every step belongs to exactly one window.

    Derived by walking the distance axis, not by dividing by a mean spacing.
    Mean spacing lies on any recording whose rate varies against the ground it
    covers: one 1 Hz phone carried for 5 km on foot and ridden for 15 km has a
    mean of 5.4 m and actual spacings of 1.4 m and 8 m, so a fixed sample count
    asks the relief question over 190 m in the walk and 1088 m in the ride. That
    is the #376 mistake — a window that means different things on different
    parts of the same track — and it cost 60 m on a steady drag that should have
    been left alone entirely. A stop is the same trap in the limit: 4000
    stationary samples make every later window a kilometre wide.
    """
    if not distances_km or len(distances_km) != count:
        # No distance axis: two windows, which is the most that can be said.
        step = max(3, count // 2)
        for lo in range(0, count - 1, step):
            yield lo, min(count, lo + step + 1)
        return

    lo = 0
    while lo < count - 1:
        limit = distances_km[lo] + window_m / 1000.0
        hi = lo + 1
        while hi < count and distances_km[hi] < limit:
            hi += 1
        # At least three samples to a verdict, and always one past the end.
        hi = min(count, max(hi, lo + 3) + 1)
        yield lo, hi
        lo = hi - 1


def _relief_span(
    lo: int, hi: int, count: int, window_m: float,
    distances_km: Optional[List[float]],
) -> slice:
    """Which samples the relief question is asked over for window ``[lo, hi)``.

    Normally the window itself. The exception is the tail: a track whose length
    is not a whole number of windows ends in a stub, and a stub answers the
    relief question over whatever ground it happens to cover. Three samples
    span 10 m, where "no relief" means nothing — that flipped an otherwise
    exact no-op by up to 3 m. A 368 m stub of a 1.5% drag is subtler and worse:
    its real relief is 5.5 m, close enough to the threshold that the verdict
    turns on rounding, and it read flat.

    So any window that does not span a full ``window_m`` of ground is judged
    over the last full window of ground instead, while still contributing only
    its own steps. A threshold in metres only means something against a fixed
    length of ground.
    """
    if distances_km and len(distances_km) == count and hi - lo >= 2:
        spanned_m = (distances_km[hi - 1] - distances_km[lo]) * 1000.0
        if spanned_m < window_m:
            back = lo
            floor = distances_km[hi - 1] - window_m / 1000.0
            while back > 0 and distances_km[back] > floor:
                back -= 1
            return slice(back, hi)
    return slice(lo, hi)


@dataclass
class TrackMetrics:
    distance: float               # metres
    total_elevation_gain: float   # metres
    elev_high: Optional[float]
    elev_low: Optional[float]
    start_latlng: Optional[List[float]]
    end_latlng: Optional[List[float]]
    average_speed: float          # m/s
    moving_time: int              # seconds (apportioned)
    elapsed_time: int             # seconds (apportioned)


def implausible_track(metrics: "TrackMetrics", elapsed_s: Optional[float]) -> Optional[str]:
    """Why a track the app is about to store is implausible, or None.

    Its figures must stay inside the bounds the trip-file import enforces
    (src/models/value_bounds.py), or the trip could not be exported and
    imported back. Only garbage reaches them: a real track never does.
    """
    if metrics.distance > DISTANCE_MAX_M:
        return "This track is longer than 100,000 km, which no real track is."
    if metrics.total_elevation_gain > GAIN_MAX_M:
        return "This track climbs more than 10,000 km, which no real track does."
    return implausible_span(elapsed_s)


def implausible_span(elapsed_s: Optional[float]) -> Optional[str]:
    """Why a track's span is implausible, or None. Separate from the rest of
    :func:`implausible_track` because it depends on the times alone, which the
    user may still correct before the track is stored."""
    if elapsed_s is not None and elapsed_s > DURATION_MAX_S:
        return "This track spans more than 31 years, which no real track does."
    return None


def recompute_track_metrics(
    points: List[TrackPoint],
    *,
    original_distance_m: float = 0.0,
    original_moving_time: int = 0,
    original_elapsed_time: int = 0,
) -> TrackMetrics:
    """Recompute an activity's scalar metrics from an edited point list.

    ``original_*`` are the pre-edit distance / times; moving and elapsed times
    are apportioned to the fraction of the original distance retained (no
    per-point time stream exists, so proportional-to-distance is the best
    available estimate).  Degenerate inputs (0 or 1 point) yield all-zero
    metrics with ``None`` latlngs/elevations.
    """
    if len(points) < 2:
        start = ([points[0].lat, points[0].lng] if points else None)
        end = start
        elev = points[0].elev if points else None
        return TrackMetrics(
            distance=0.0,
            total_elevation_gain=0.0,
            elev_high=elev,
            elev_low=elev,
            start_latlng=start,
            end_latlng=end,
            average_speed=0.0,
            moving_time=0,
            elapsed_time=0,
        )

    # Cumulative distance is accumulated over EVERY point, but only recorded
    # alongside points that carry an elevation — so a stretch recorded without
    # one leaves a real gap in the series the smoothing window can see, rather
    # than two distant samples looking adjacent.
    distance_km = 0.0
    elevs: List[float] = []
    elev_dist_km: List[float] = []
    if points[0].elev is not None:
        elevs.append(points[0].elev)
        elev_dist_km.append(0.0)
    for i in range(1, len(points)):
        distance_km += haversine_km(
            points[i - 1].lat, points[i - 1].lng, points[i].lat, points[i].lng)
        if points[i].elev is not None:
            elevs.append(points[i].elev)
            elev_dist_km.append(distance_km)
    distance_m = distance_km * 1000.0

    gain = elevation_gain(elevs, elev_dist_km)
    elev_high = max(elevs) if elevs else None
    elev_low = min(elevs) if elevs else None

    # Apportion times proportionally to retained distance.
    if original_distance_m > 0:
        frac = max(0.0, min(1.0, distance_m / original_distance_m))
    else:
        frac = 1.0
    moving_time = int(round(original_moving_time * frac))
    elapsed_time = int(round(original_elapsed_time * frac))

    average_speed = distance_m / moving_time if moving_time > 0 else 0.0

    return TrackMetrics(
        distance=distance_m,
        total_elevation_gain=gain,
        elev_high=elev_high,
        elev_low=elev_low,
        start_latlng=[points[0].lat, points[0].lng],
        end_latlng=[points[-1].lat, points[-1].lng],
        average_speed=average_speed,
        moving_time=moving_time,
        elapsed_time=elapsed_time,
    )
