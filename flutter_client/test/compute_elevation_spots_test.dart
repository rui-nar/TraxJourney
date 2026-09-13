// computeElevationSpots — the concatenate+LTTB-downsample computation
// elevation_chart.dart runs via `compute()` on a background isolate when the
// unfiltered (no activity selected) elevation series is large. See
// elevation_chart.dart's doc comment on computeElevationSpots for why: a
// long trip's full elevation payload, concatenated across every activity,
// can be tens of thousands of points, and doing that synchronously on the
// UI isolate is the same mistake the day-carousel ANR fix corrected
// elsewhere (map_panel.dart's buildDayIndex). This only exercises the pure
// computation itself, same scope as build_day_index_test.dart.

import 'dart:math' as math;

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/elevation_chart.dart';

void main() {
  Map<String, dynamic> activity(dynamic id, List<List<num>> profile) =>
      {'id': id, 'elevation_profile': profile};

  test('concatenates activities in order, offsetting distance by the '
      'previous activity\'s total', () {
    final result = computeElevationSpots((
      activities: [
        activity(1, [
          [0.0, 100],
          [5.0, 200],
        ]),
        activity(2, [
          [0.0, 150],
          [3.0, 50],
        ]),
      ],
      selectedId: null,
    ));

    expect(result.spots.map((s) => (s.x, s.y)).toList(), [
      (0.0, 100.0),
      (5.0, 200.0),
      (5.0, 150.0), // second activity's 0.0 offset by the first's 5.0 total
      (8.0, 50.0),
    ]);
  });

  test('minY/maxY span the full series, not just the downsampled points', () {
    final result = computeElevationSpots((
      activities: [
        activity(1, [
          [0.0, 10],
          [1.0, -5],
          [2.0, 40],
          [3.0, 0],
        ]),
      ],
      selectedId: null,
    ));
    expect(result.minY, -5);
    expect(result.maxY, 40);
  });

  test('selectedId filters to just that activity', () {
    final result = computeElevationSpots((
      activities: [
        activity(1, [
          [0.0, 100],
          [5.0, 200],
        ]),
        activity(2, [
          [0.0, 999],
          [3.0, 999],
        ]),
      ],
      selectedId: 2,
    ));
    // Not offset by activity 1 — it was filtered out entirely, not just hidden.
    expect(result.spots.map((s) => (s.x, s.y)).toList(), [
      (0.0, 999.0),
      (3.0, 999.0),
    ]);
  });

  test('empty activities yields empty spots and zeroed bounds', () {
    final result = computeElevationSpots((activities: const [], selectedId: null));
    expect(result.spots, isEmpty);
    expect(result.minY, 0);
    expect(result.maxY, 0);
  });

  test('a series over the downsample threshold is capped, keeping the '
      'first and last point', () {
    final profile = List.generate(1000, (i) => [i * 0.1, i.toDouble()]);
    final result = computeElevationSpots((
      activities: [activity(1, profile)],
      selectedId: null,
    ));
    expect(result.spots.length, lessThan(1000));
    expect(result.spots.first.x, 0.0);
    expect(result.spots.last.x, closeTo(99.9, 0.001));
    // Downsampling must not distort the true range.
    expect(result.minY, 0);
    expect(result.maxY, 999);
  });

  // ── Issue #323: the chart drawn from ~300-point profiles ──
  //
  // The load no longer upgrades /meta's precomputed low-res profiles to full
  // GPS resolution. These pin that the chart is unchanged by that, against a
  // full-resolution fixture.
  //
  // [asServerDownsamples] stands in for src/project/elevation_downsample.py,
  // and deliberately makes a *weaker* selection than it does: a uniform stride
  // plus the four points that module guarantees to keep — first, last, the
  // global minimum and the global maximum, each pinned by
  // tests/test_elevation_downsample.py. The server picks its interior points
  // by LTTB instead, so what survives this stand-in survives the real thing.
  //
  // Shape is not asserted point-for-point, and could not be: LTTB over an
  // already-downsampled series does not select the same 300 samples as LTTB
  // over the raw one. What the chart renders *from* — how many spots, what the
  // y axis spans, where each activity starts on the x axis — is identical, and
  // that is what is asserted.
  List<List<num>> asServerDownsamples(List<List<num>> profile, int maxPoints) {
    if (profile.length <= maxPoints) return profile;
    var lo = 0, hi = 0;
    for (var i = 0; i < profile.length; i++) {
      if (profile[i][1] < profile[lo][1]) lo = i;
      if (profile[i][1] > profile[hi][1]) hi = i;
    }
    final keep = <int>{0, profile.length - 1, lo, hi};
    final step = (profile.length - 1) / (maxPoints - 1);
    for (var i = 0; i < maxPoints; i++) {
      keep.add((i * step).round());
    }
    final idx = keep.toList()..sort();
    return [for (final i in idx) profile[i]];
  }

  /// A trip's worth of full-resolution profiles: a different length and a
  /// different total distance per activity, so a lost or shifted per-activity
  /// offset moves the x axis.
  List<Map<String, dynamic>> fullResolutionTrip() => [
        for (var a = 0; a < 4; a++)
          activity(
            a + 1,
            List.generate(3000 + a * 500, (i) {
              final n = 3000 + a * 500;
              final t = i / (n - 1);
              return [
                t * (40.0 + a * 17.5),
                600 + 400 * math.sin(t * 9 + a) + 30 * math.sin(t * 211),
              ];
            }),
          ),
      ];

  List<Map<String, dynamic>> downsampledTrip(List<Map<String, dynamic>> full) => [
        for (final a in full)
          activity(a['id'],
              asServerDownsamples(a['elevation_profile'] as List<List<num>>, 300)),
      ];

  test('~300-point profiles render the same chart as full-resolution ones', () {
    final full = fullResolutionTrip();
    final low = downsampledTrip(full);
    // The shape the report measured, three orders of magnitude smaller: many
    // samples per activity, each reduced to ~300.
    expect((low.first['elevation_profile'] as List).length, lessThan(310));

    final before = computeElevationSpots((activities: full, selectedId: null));
    final after = computeElevationSpots((activities: low, selectedId: null));

    expect(after.spots.length, before.spots.length);
    expect(after.minY, before.minY);
    expect(after.maxY, before.maxY);
    expect(after.spots.first.x, before.spots.first.x);
    // The last x is every activity's total distance summed, so it moves if any
    // per-activity offset is lost or shifted.
    expect(after.spots.last.x, closeTo(before.spots.last.x, 1e-9));
  });

  test('a selected activity renders the same chart from its ~300 points', () {
    final full = fullResolutionTrip();
    final low = downsampledTrip(full);

    final before = computeElevationSpots((activities: full, selectedId: 3));
    final after = computeElevationSpots((activities: low, selectedId: 3));

    expect(after.minY, before.minY);
    expect(after.maxY, before.maxY);
    expect(after.spots.first.x, before.spots.first.x);
    expect(after.spots.last.x, closeTo(before.spots.last.x, 1e-9));
  });
}
