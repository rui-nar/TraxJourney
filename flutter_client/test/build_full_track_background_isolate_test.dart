// Regression tests for the background-isolate branch of
// ProjectNotifier._buildFullTrack (issue #276) and the _buildFullTrackGen
// staleness guard it relies on. build_full_track_test.dart only drives the
// pure buildFullTrackResult() computation directly; project_notifier_camera_
// idle_elevation_test.dart only ever feeds it activities small enough to take
// the inline branch. Neither exercises the actual compute()-backed path, nor
// the generation counter meant to discard a superseded result — this file
// covers both, via the @visibleForTesting buildFullTrack()/buildFullTrackGen
// seam on ProjectNotifier (no network mocking needed: activities/geo are
// plain public fields).

import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

Map<String, dynamic> _activity(String id, int points) => {
      'id': id,
      'elevation_profile': [
        for (var i = 0; i < points; i++) [i * 0.01, 100 + i.toDouble()],
      ],
    };

Map<String, dynamic> _geoFeature(String activityId, int points, double lonBase) => {
      'type': 'Feature',
      'properties': {'type': 'activity', 'activity_id': activityId},
      'geometry': {
        'type': 'LineString',
        'coordinates': [
          for (var i = 0; i < points; i++) [lonBase + i * 0.001, 45.0 + i * 0.001],
        ],
      },
    };

Map<String, dynamic> _geo(List<Map<String, dynamic>> features) =>
    {'type': 'FeatureCollection', 'features': features};

void main() {
  // Comfortably above kInlineFullTrackThreshold (5000) so the background
  // compute() branch — not the inline one — is the one under test.
  const largePoints = 6000;

  test(
      '_buildFullTrack takes the background-isolate branch for a large trip '
      'and produces the same result as the inline computation would',
      () async {
    final activities = [_activity('1', largePoints)];
    final geo = _geo([_geoFeature('1', largePoints, 7.0)]);
    expect(totalElevationProfilePoints(activities), greaterThan(kInlineFullTrackThreshold));

    final notifier = ProjectNotifier(ProjectService());
    notifier.activities = activities;
    notifier.geo = geo;
    await notifier.buildFullTrack();

    final expected = buildFullTrackResult((geo: geo, activities: activities));
    expect(notifier.fullTrack, expected.fullTrack);
    expect(notifier.perActivityTracks['1'], expected.perActivityTracks['1']);
    expect(notifier.fullTrack, isNotEmpty);
  });

  test('_buildFullTrackGen is bumped on every call, inline branch included',
      () async {
    // Directly testable without needing real isolate timing — see this
    // file's header and the next test's comment for why the full
    // background-isolate-vs-inline race isn't driven end-to-end here.
    final notifier = ProjectNotifier(ProjectService());
    final genBefore = notifier.buildFullTrackGen;

    notifier.activities = [_activity('1', 2)]; // well under the threshold
    notifier.geo = _geo([_geoFeature('1', 2, 7.0)]);
    await notifier.buildFullTrack();
    expect(notifier.buildFullTrackGen, genBefore + 1);

    await notifier.buildFullTrack();
    expect(notifier.buildFullTrackGen, genBefore + 2);
  });

  test(
      'a superseded background-isolate compute() does not clobber a later '
      'inline result (regression for the stale-generation bug)', () async {
    // This drives a real compute() isolate round-trip rather than the pure
    // computation, unlike this codebase's usual convention (see
    // map_panel_polyline_decimation_test.dart's header) — the previous test
    // already covers the deterministic, non-timing-dependent half of this
    // guard (gen incremented on every call); this test additionally proves
    // the guard actually prevents the clobber, which requires a real
    // in-flight compute() to race against. It stays deterministic because
    // the "newer" call below is the synchronous inline branch, not a second
    // isolate — there is no dependency on how long either compute() takes
    // relative to the other.
    final largeActivities = [_activity('large', largePoints)];
    final largeGeo = _geo([_geoFeature('large', largePoints, 7.0)]);
    final smallActivities = [_activity('small', 2)];
    final smallGeo = _geo([_geoFeature('small', 2, 20.0)]);

    final notifier = ProjectNotifier(ProjectService());
    notifier.activities = largeActivities;
    notifier.geo = largeGeo;
    // Start the large-trip build but don't await it yet — it's now in flight
    // on a background isolate.
    final stalePending = notifier.buildFullTrack();

    // The notifier gets reused for a different (small) project before that
    // finishes — its own _buildFullTrack() call takes the inline branch and
    // must still bump the generation counter (bug fix under test).
    notifier.activities = smallActivities;
    notifier.geo = smallGeo;
    await notifier.buildFullTrack();

    final expectedSmall = buildFullTrackResult((geo: smallGeo, activities: smallActivities));
    expect(notifier.fullTrack, expectedSmall.fullTrack,
        reason: 'the inline call must win immediately');

    // Now let the stale large-trip compute() resolve.
    await stalePending;
    expect(notifier.fullTrack, expectedSmall.fullTrack,
        reason: 'the stale large-trip result must not overwrite the newer '
            'small-trip one once it resolves');
    expect(notifier.perActivityTracks.containsKey('large'), isFalse);
  });
}
