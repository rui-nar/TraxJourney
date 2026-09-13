// Issue #294 — changing the selection must not redo geometry work.
//
// `_cachedAllPoints` is derived from `geo` alone and is consumed only by the
// one-shot fit-to-bounds, but it sat in the selection-dependent half of
// MapPanel.build(). Every day/activity tap therefore rebuilt a list of every
// point in the trip for nothing: measured at ~24 ms per selection on a
// 500k-point trip — over the frame budget on its own, before anything else
// the tap triggers. Moving it into the geo-dependent half took the same
// measurement to ~0.7 ms.
//
// Asserted structurally via the Phase 0 spans (#291) rather than by timing:
// `build_specs` wraps all geo-derived work, so "it ran exactly once across
// several selection changes" is the invariant, and it holds on any machine.

import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_geometry_memo.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _acts = 6;
const _pts = 40;

Map<String, dynamic> _geo() => {
      'type': 'FeatureCollection',
      'features': [
        for (var a = 0; a < _acts; a++)
          {
            'type': 'Feature',
            'properties': {
              'type': 'activity',
              'activity_id': a,
              'sport_type': 'Ride',
            },
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                for (var i = 0; i < _pts; i++)
                  [7.0 + a * 0.1 + i * 1e-4, 45.0 + a * 0.1 + i * 1e-4]
              ],
            },
          },
      ],
    };

ProjectNotifier _notifier() => ProjectNotifier(ProjectService())
  ..ref = const ProjectRef(name: 'Trip')
  ..geo = _geo()
  ..activities = [
    for (var a = 0; a < _acts; a++)
      {'id': a, 'start_date_local': '2026-06-0${a + 1}T08:00:00'}
  ]
  ..items = [
    for (var a = 0; a < _acts; a++) {'item_type': 'activity', 'activity_id': a}
  ]
  ..isLoading = false;

void main() {
  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    perfSpans
      ..reset()
      ..enabled = true;
  });

  tearDown(() {
    perfSpans
      ..reset()
      ..enabled = true;  // the library default — see PerfSpans.enabled
  });

  testWidgets('changing the selection does no geo-derived work', (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final notifier = _notifier();
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: notifier,
          builder: (_, __) => MapPanel(
            notifier: notifier,
            mapController: controller,
            basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          ),
        ),
      ),
    ));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    expect(perfSpans.blockingSpans['build_specs'], hasLength(1),
        reason: 'the initial build must derive geometry exactly once');
    expect(perfSpans.blockingSpans['all_points'], hasLength(1));

    for (var d = 1; d <= _acts; d++) {
      notifier.selectedDays = {'2026-06-0$d'};
      notifier.notifyListeners();
      await tester.pump();
    }

    expect(perfSpans.blockingSpans['build_specs'], hasLength(1),
        reason: '$_acts selection changes must not rebuild geometry specs');
    // This is the assertion that actually pins #294's fix: before it,
    // _cachedAllPoints lived in the selection-dependent half and this span
    // would report _acts + 1 samples instead of 1.
    expect(perfSpans.blockingSpans['all_points'], hasLength(1),
        reason: '$_acts selection changes must not rebuild the all-points '
            'list, which depends only on geo');
    // The restyle itself must still be happening — otherwise the assertion
    // above would also pass on a map that simply stopped updating.
    expect(perfSpans.blockingSpans['style_markers'], hasLength(_acts + 1));
  });

  testWidgets('a geo swap does redo it, exactly once', (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final notifier = _notifier();
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: notifier,
          builder: (_, __) => MapPanel(
            notifier: notifier,
            mapController: controller,
            basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          ),
        ),
      ),
    ));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    notifier.geo = _geo(); // the low-res -> full-res upgrade
    notifier.notifyListeners();
    await tester.pump();

    expect(perfSpans.blockingSpans['build_specs'], hasLength(2),
        reason: 'new geometry must be picked up — the caching above must not '
            'be so aggressive that a real upgrade is missed');
  });

  test('seeding both caches makes a cold derivation free', () {
    final geo = _geo();
    final coords =
        (geo['features'] as List).first['geometry']['coordinates'] as List;

    coordsConversionCount = 0;
    seedCoordsLatLng(coords, coordsToLatLng(coords));
    final mid = arcMidpoint(coords);
    seedArcMidpoint(coords, mid!);

    memoCoordsToLatLng(coords);
    expect(coordsConversionCount, 0);
    expect(memoArcMidpoint(coords), mid);
  });
}
