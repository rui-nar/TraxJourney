// Smoke test for the async polyline-decimation wiring in map_panel.dart
// (_maybeDecimatePolylines) — the async compute() round-trip itself isn't
// exercised here (this codebase's established convention, per
// hit_test_map_tap_test.dart/compute_elevation_spots_test.dart, is to cover
// the pure compute()-target function directly — see
// decimate_polyline_points_test.dart — rather than drive a real isolate
// round-trip through a widget test). This just guards that wiring a large
// trip's geo into MapPanel doesn't throw, and that the fast/sync path still
// renders full fidelity on the very first frame (decimation only ever
// swaps in later, asynchronously — never truncates synchronously).

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

List<List<double>> _denseCoords(int n, double lonOffset) => [
      for (var i = 0; i < n; i++) [lonOffset + i * 0.0001, 45.0 + i * 0.0001],
    ];

Map<String, dynamic> _largeGeo() => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': '1'},
          'geometry': {
            'type': 'LineString',
            'coordinates': _denseCoords(4000, 0),
          },
        },
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': '2'},
          'geometry': {
            'type': 'LineString',
            'coordinates': _denseCoords(4000, 10),
          },
        },
      ],
    };

int _totalRenderedPoints(WidgetTester tester) => tester
    .widgetList<PolylineLayer>(find.byType(PolylineLayer))
    .expand((l) => l.polylines)
    .fold<int>(0, (sum, p) => sum + p.points.length);

void main() {
  testWidgets(
      'a trip whose combined polyline point count is well above the '
      'decimation budget still renders (at full fidelity on the first '
      'frame) without throwing', (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final notifier = ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..geo = _largeGeo()
      ..isLoading = false;
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: MapPanel(
          notifier: notifier,
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
        ),
      ),
    ));
    await tester.pump();

    expect(_totalRenderedPoints(tester), 8000);

    // Let _fitBoundsOnce's animatedFitCamera (scheduled from a post-frame
    // callback) finish before the test ends — mirrors
    // map_panel_fit_bounds_test.dart's _settleFit. Without this the
    // AnimationController's ticker is still registered when the widget tree
    // is torn down, which the scheduler flags as a leaked animation.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));
  });
}
