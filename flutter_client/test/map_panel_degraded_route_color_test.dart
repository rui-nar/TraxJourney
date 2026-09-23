// A degraded segment (route_degraded=true — the server fell back to a
// straight endpoint chord, see api/segments.py) is a rendering gap on the
// map: route_degraded was already emitted per-feature by client_geo_builder,
// but nothing consumed it, so a degraded segment looked identical to a fully
// resolved one (issue #207). Fixed with a grey line colour, not a line-style
// change.

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _kDegradedRouteColor = Color(0xFF64748B); // must match map_panel.dart

Map<String, dynamic> _geo({required bool degraded}) => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {
            'type': 'segment',
            'segment_id': 'seg-1',
            'segment_type': 'boat',
            'route_mode': 'ferry',
            'route_degraded': degraded,
          },
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [11.35, 47.25],
              [11.55, 47.35],
            ],
          },
        },
      ],
    };

ProjectNotifier _notifier({required bool degraded}) =>
    ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..geo = _geo(degraded: degraded)
      ..items = [
        {
          'item_type': 'segment',
          'segment': {'id': 'seg-1', 'segment_type': 'boat'},
        },
      ]
      ..isLoading = false;

/// Mirrors map_panel_fit_bounds_test.dart's harness.
Widget _panel(ProjectNotifier notifier, AnimatedMapController controller) =>
    MaterialApp(
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
    );

void main() {
  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
  });

  Future<Polyline> pumpAndGetSegmentLine(
      WidgetTester tester, {required bool degraded}) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(_panel(_notifier(degraded: degraded), controller));
    // _fitBoundsOnce animates the initial camera fit (AnimatedMapController):
    // one pump for the build + post-frame callback, a second for the ticker's
    // zero-elapsed first tick, then the animation's own duration — otherwise
    // its ticker is still running when the test tears down (mirrors
    // map_panel_fit_bounds_test.dart's _settleFit).
    await tester.pump();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    final layer = tester.widget<PolylineLayer>(find.byType(PolylineLayer).first);
    expect(layer.polylines, hasLength(1));
    return layer.polylines.single;
  }

  testWidgets('a degraded segment is drawn in grey', (tester) async {
    final line = await pumpAndGetSegmentLine(tester, degraded: true);
    expect(line.color, _kDegradedRouteColor);
  });

  testWidgets('a fully resolved segment keeps its normal colour, not grey',
      (tester) async {
    final line = await pumpAndGetSegmentLine(tester, degraded: false);
    expect(line.color, isNot(_kDegradedRouteColor));
  });
}
