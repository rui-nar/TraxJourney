// Issue #295. `buildFullTrackResult` paired `profile[i]` with `coords[i]`
// whenever there were at least as many coordinates as profile samples. That is
// only correct when the two are index-aligned — i.e. when the profile is at
// full GPS resolution.
//
// Given a downsampled profile it mapped the whole distance range onto the
// leading fraction of the geometry: a straight 10 km track with a 10-sample
// profile ended at lat 0.009 instead of 0.099, so the map cursor pointed at
// roughly a tenth of the way along. Measured before the fix.
//
// That is *why* the client fetched the ~33 MB full-resolution details payload:
// the low-res profile `/meta` already carries could not be used. Distances now
// come from the geometry and are scaled to the profile's total, so the only
// thing needed from the profile is that one number, at any resolution.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';

/// A straight track of [n] coordinates running north from the equator.
List<List<double>> _coords(int n) => [for (var i = 0; i < n; i++) [0.0, i * 0.001]];

Map<String, dynamic> _geo(List<List<double>> coords) => {
      'features': [
        {
          'properties': {'activity_id': '1'},
          'geometry': {'coordinates': coords},
        }
      ],
    };

/// A profile of [n] samples spanning [totalKm].
List<List<double>> _profile(int n, double totalKm) =>
    [for (var i = 0; i < n; i++) [i * (totalKm / (n - 1)), 100.0 + i]];

({List<(double, GeoPoint)> fullTrack, Map<String, List<(double, GeoPoint)>> perActivityTracks})
    _build(List<List<double>> coords, List<List<double>> profile) =>
        buildFullTrackResult((
          geo: _geo(coords),
          activities: [
            {'id': 1, 'elevation_profile': profile}
          ],
        ));

void main() {
  test('a downsampled profile still spans the whole geometry', () {
    final coords = _coords(100);
    final r = _build(coords, _profile(10, 10.0));

    // The track must reach the last coordinate, not the tenth one.
    expect(r.fullTrack.last.$2.lat, closeTo(coords.last[1], 1e-9));
    expect(r.fullTrack.first.$2.lat, closeTo(coords.first[1], 1e-9));
  });

  test('distance still ends at the profile total, whatever its resolution', () {
    // The chart's X axis comes from the profile, so the track has to share
    // that scale or the cursor and the chart disagree.
    for (final samples in [5, 10, 50, 100]) {
      final r = _build(_coords(100), _profile(samples, 10.0));
      expect(r.fullTrack.last.$1, closeTo(10.0, 1e-6),
          reason: 'with $samples profile samples');
    }
  });

  test('a full-resolution profile is unchanged by the fix', () {
    final coords = _coords(100);
    final r = _build(coords, _profile(100, 10.0));
    expect(r.fullTrack.last.$2.lat, closeTo(coords.last[1], 1e-9));
    expect(r.fullTrack.last.$1, closeTo(10.0, 1e-6));
  });

  test('track resolution follows the geometry, not the profile', () {
    // Cursor accuracy is a function of how finely the *positions* are known,
    // which the low-res profile never limited — it only ever supplied the
    // distance scale.
    final r = _build(_coords(100), _profile(10, 10.0));
    expect(r.fullTrack, hasLength(100));
  });

  test('distances increase monotonically', () {
    final r = _build(_coords(100), _profile(10, 10.0));
    for (var i = 1; i < r.fullTrack.length; i++) {
      expect(r.fullTrack[i].$1, greaterThanOrEqualTo(r.fullTrack[i - 1].$1));
    }
  });

  test('an activity with no profile contributes nothing', () {
    final r = buildFullTrackResult((
      geo: _geo(_coords(10)),
      activities: [
        {'id': 1}
      ],
    ));
    expect(r.fullTrack, isEmpty);
  });

  test('per-activity tracks are keyed and span their own geometry', () {
    final coords = _coords(100);
    final r = _build(coords, _profile(10, 10.0));
    expect(r.perActivityTracks['1'], hasLength(100));
    expect(r.perActivityTracks['1']!.last.$2.lat, closeTo(coords.last[1], 1e-9));
  });
}
