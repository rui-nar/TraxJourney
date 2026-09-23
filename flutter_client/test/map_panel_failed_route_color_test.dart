// A segment whose route resolution ended in route_status="failed" (e.g. no
// ferry route exists, or the resolve never got a verdict within the client's
// poll window) used to render as an ordinary great-circle line forever —
// indistinguishable from an intentionally straight segment, discoverable only
// by scrolling to the tile list's "tap to retry" row.
// Fixed with a distinct line colour on the map itself, mirroring the existing
// degraded-route treatment (map_panel_degraded_route_color_test.dart).

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _kFailedRouteColor = Color(0xFFDC2626); // must match map_panel.dart
const _kDegradedRouteColor = Color(0xFF64748B); // must match map_panel.dart

Map<String, dynamic> _geo({required String routeStatus}) => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {
            'type': 'segment',
            'segment_id': 'seg-1',
            'segment_type': 'boat',
            'route_mode': 'great_circle',
            'route_degraded': false,
            'route_status': routeStatus,
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

ProjectNotifier _notifier({required String routeStatus}) =>
    ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..geo = _geo(routeStatus: routeStatus)
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
      WidgetTester tester, {required String routeStatus}) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(
        _panel(_notifier(routeStatus: routeStatus), controller));
    await tester.pump();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    final layer = tester.widget<PolylineLayer>(find.byType(PolylineLayer).first);
    expect(layer.polylines, hasLength(1));
    return layer.polylines.single;
  }

  testWidgets('a failed segment is drawn in red', (tester) async {
    final line = await pumpAndGetSegmentLine(tester, routeStatus: 'failed');
    expect(line.color, _kFailedRouteColor);
  });

  testWidgets('a resolved segment keeps its normal colour, not the failed red',
      (tester) async {
    final line = await pumpAndGetSegmentLine(tester, routeStatus: 'resolved');
    expect(line.color, isNot(_kFailedRouteColor));
    expect(line.color, isNot(_kDegradedRouteColor));
  });
}
