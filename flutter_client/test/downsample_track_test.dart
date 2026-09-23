// Issue #276, option 1 of the agreed plan.
//
// `fullTrack` and `perActivityTracks` exist to map a distance to a position —
// for the map-to-chart cursor, and for hitTestMapTap's nearest-point scan.
// They draw nothing; the rendered polylines come from `geo` and are capped
// separately by kMaxTotalPolylinePoints.
//
// The device diagnostics measured 1,465,345 points in each, on a 219-activity
// trip: roughly 200 MB held to answer a question a fraction of that resolves,
// and a linear scan of 1.5 M points on every map tap. It regressed in #295,
// where removing the index-aligned pairing made the track follow the geometry
// (~6,700 points per activity) rather than the elevation profile (~300).

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';

List<(double, GeoPoint)> _track(int n) => [
      for (var i = 0; i < n; i++)
        (i * 0.01, (lat: 45.0 + i * 0.0001, lon: 7.0 + i * 0.0001)),
    ];

void main() {
  test('a track under the cap is returned untouched', () {
    final t = _track(50);
    expect(identical(downsampleTrack(t, 100), t), isTrue,
        reason: 'no copy, no allocation, when there is nothing to do');
  });

  test('a track over the cap is reduced to exactly the cap', () {
    expect(downsampleTrack(_track(10000), 1000), hasLength(1000));
  });

  test('the endpoints are preserved exactly', () {
    // The distance range is the chart's X axis; losing either end would make
    // the cursor and the chart disagree at the extremes.
    final t = _track(10000);
    final d = downsampleTrack(t, 1000);
    expect(d.first, t.first);
    expect(d.last, t.last);
  });

  test('distances stay monotonic', () {
    final d = downsampleTrack(_track(10000), 1000);
    for (var i = 1; i < d.length; i++) {
      expect(d[i].$1, greaterThan(d[i - 1].$1));
    }
  });

  test('sampling is uniform, so no stretch of track loses its cursor', () {
    // A stride that bunched points at one end would leave the rest of the
    // activity resolving to a distant marker.
    final d = downsampleTrack(_track(10000), 101);
    var maxGap = 0.0;
    for (var i = 1; i < d.length; i++) {
      final gap = d[i].$1 - d[i - 1].$1;
      if (gap > maxGap) maxGap = gap;
    }
    // Ideal spacing is 100 source points (1.0 km); allow a rounding point.
    expect(maxGap, lessThan(1.1));
  });

  test('a degenerate cap returns the track rather than mangling it', () {
    final t = _track(10);
    expect(downsampleTrack(t, 1), same(t));
    expect(downsampleTrack(t, 0), same(t));
  });

  test('an empty or single-point track survives', () {
    expect(downsampleTrack(const [], 100), isEmpty);
    expect(downsampleTrack(_track(1), 100), hasLength(1));
  });

  test('the trip-wide cap keeps a long trip proportionate', () {
    // 219 activities at ~6,700 points each measured 1.47 M; at the cap that
    // is ~219,000 — an order of magnitude less, for the same job.
    expect(kMaxTrackPointsPerActivity, lessThanOrEqualTo(2000));
    expect(kMaxTrackPointsPerActivity, greaterThanOrEqualTo(300),
        reason: 'below the chart own 300-point budget there would be '
            'visible cursor stepping');
  });
}
