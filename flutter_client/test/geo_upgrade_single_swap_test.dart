// Issue #293 — the low-res -> full-res geo upgrade must reach the UI as ONE
// swap, not a staged reveal. See docs/PERF_MAP_LOAD.md.
//
// _loadFullGeoProgressively used to replace the track a few activities at a
// time, notifying after each batch (~8 repaints, 80 ms apart). Every one of
// those notifications rebuilds the whole tree — each polyline spec, each
// marker spec, a compute() decimation hop, ActivityPanel, ElevationChart —
// which is what issue #276 reported as "small local blocks and unblocks": a
// metronome of hitches, because the cause was literally a metronome.
//
// The invariant pinned here is deliberately about what the UI *observes*, not
// about how the notifier is implemented: across a whole load, listeners may
// only ever be handed two distinct geo objects — the low-res one, then the
// full-res one.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

/// 24 activities: enough that the old batched reveal would have produced its
/// full ~8 repaints (it capped batches at 8 regardless of trip size), so a
/// regression here shows up as 8 extra geo objects rather than one or two.
const _activityCount = 24;

List<Map<String, dynamic>> _activities() => [
      for (var i = 0; i < _activityCount; i++)
        {
          'id': 100 + i,
          'name': 'Leg $i',
          'type': 'Ride',
          'start_date_local': '2026-05-${(i % 28) + 1}T08:00:00',
        },
    ];

List<Map<String, dynamic>> _items() => [
      for (var i = 0; i < _activityCount; i++)
        {'item_type': 'activity', 'activity_id': 100 + i},
    ];

Map<String, dynamic> _geo({required bool full}) => {
      'type': 'FeatureCollection',
      'features': [
        for (var i = 0; i < _activityCount; i++)
          {
            'type': 'Feature',
            'properties': {'activity_id': '${100 + i}'},
            'geometry': {
              'type': 'LineString',
              // Low-res is a straight two-point line; full-res has real shape.
              'coordinates': full
                  ? [
                      for (var p = 0; p < 12; p++) [1.0 + p * 0.01, 2.0 + p * 0.01]
                    ]
                  : [
                      [1.0, 2.0],
                      [1.11, 2.11],
                    ],
            },
          },
      ],
    };

class _Service extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'lock_version': 1,
        'name': 'Trip',
        'activities': _activities(),
        'items': _items(),
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      _geo(full: false);

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) async =>
      {'name': 'Trip', 'activities': _activities(), 'items': _items()};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _geo(full: true);
}

/// Whether the full-res swap has actually landed. Deliberately observed from
/// the geo itself rather than from `isGeoLoaded`, which starts life `true`
/// (it flags a degraded upgrade, not completion) and so would make every
/// wait below pass instantly and vacuously.
bool _upgraded(ProjectNotifier n) {
  final features = n.geo?['features'] as List?;
  if (features == null || features.isEmpty) return false;
  return (features.first['geometry']['coordinates'] as List).length == 12;
}

/// Polls [cond] up to [timeout]; returns whether it came true.
Future<bool> _waitFor(bool Function() cond,
    {Duration timeout = const Duration(seconds: 2)}) async {
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    if (cond()) return true;
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
  return cond();
}

void main() {
  setUp(() => projectDataCache.resetForTest());

  test('the full-res upgrade reaches listeners as a single geo swap',
      () async {
    final notifier = ProjectNotifier(_Service());

    // Record the identity of every distinct geo object listeners are shown.
    final seen = <Map<String, dynamic>>[];
    notifier.addListener(() {
      final g = notifier.geo;
      if (g == null) return;
      if (seen.isEmpty || !identical(seen.last, g)) seen.add(g);
    });

    await notifier.load(_ref);
    // Phase 2 runs unawaited in the background.
    expect(await _waitFor(() => _upgraded(notifier)), isTrue,
        reason: 'the full-res upgrade never arrived');
    // Settle: catch any further swaps the upgrade might still emit.
    await Future<void>.delayed(const Duration(milliseconds: 50));

    expect(seen, hasLength(2),
        reason: 'listeners saw ${seen.length} distinct geo objects; the '
            'upgrade must be low-res then one atomic full-res swap');

    // And the swap really did deliver full resolution, so the count above
    // cannot be satisfied by simply never upgrading.
    final coords =
        (notifier.geo!['features'] as List).first['geometry']['coordinates'] as List;
    expect(coords, hasLength(12));
  });

  test('a camera-idle wait does not stall the upgrade past its cap', () async {
    final notifier = ProjectNotifier(_Service())
      ..cameraIdleTimeout = const Duration(milliseconds: 50);

    // Camera never settles — the user is panning continuously.
    notifier.setMapCameraActive(true);

    await notifier.load(_ref);
    expect(await _waitFor(() => _upgraded(notifier)), isTrue,
        reason: 'a user who never stops panning must still get full-res geo');
  });

  test('the upgrade lands as soon as the camera settles, without polling',
      () async {
    // The cap is deliberately far longer than the test will wait: if the
    // upgrade still arrives, it was released by the camera-idle event rather
    // than by the timeout expiring.
    final notifier = ProjectNotifier(_Service())
      ..cameraIdleTimeout = const Duration(seconds: 30);
    notifier.setMapCameraActive(true);

    await notifier.load(_ref);
    expect(
        await _waitFor(() => _upgraded(notifier),
            timeout: const Duration(milliseconds: 150)),
        isFalse,
        reason: 'the upgrade must be held while the camera is moving');

    notifier.setMapCameraActive(false);
    expect(
        await _waitFor(() => _upgraded(notifier),
            timeout: const Duration(milliseconds: 500)),
        isTrue,
        reason: 'settling the camera must release the upgrade immediately, '
            'well inside the 30 s cap it would otherwise wait for');
  });
}
