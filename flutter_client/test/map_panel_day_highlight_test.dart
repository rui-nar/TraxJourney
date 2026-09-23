// Issue #199: the view-mode day carousel selects a day via
// notifier.selectDays(), but MapPanel (unlike ManageMapPanel) never read
// selectedDay/selectedDays, so nothing on the map ever highlighted. Fixed by
// porting ManageMapPanel's effectiveDays/dayActIds/daySegIds computation
// (reusing its ManageMapPanelState._dayItemIds helper) into MapPanel's own
// build(). This guards that port: without it, both polylines below would
// come back in their neutral (non-dimmed) colour regardless of selectedDays.

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _kTrackColor = Color(0xFF6B7280); // ProjectNotifier.trackColor default

Map<String, dynamic> _geo() => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': 1},
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [11.0, 47.0],
              [11.1, 47.1],
            ],
          },
        },
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': 2},
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [12.0, 48.0],
              [12.1, 48.1],
            ],
          },
        },
      ],
    };

ProjectNotifier _notifier({required String selectedDay}) =>
    ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..geo = _geo()
      ..activities = [
        {'id': 1, 'start_date_local': '2026-06-01T08:00:00'},
        {'id': 2, 'start_date_local': '2026-06-02T08:00:00'},
      ]
      ..items = [
        {'item_type': 'activity', 'activity_id': 1},
        {'item_type': 'activity', 'activity_id': 2},
      ]
      ..isLoading = false
      ..selectedDays = {selectedDay};

/// Mirrors map_panel_fit_bounds_test.dart's / map_panel_degraded_route_color_test.dart's harness.
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

  testWidgets(
      "selecting a day highlights that day's activity and dims the other",
      (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(
        _panel(_notifier(selectedDay: '2026-06-01'), controller));
    // _fitBoundsOnce animates the initial camera fit — see
    // map_panel_degraded_route_color_test.dart for why three pumps.
    await tester.pump();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    final layer = tester.widget<PolylineLayer>(find.byType(PolylineLayer).first);
    expect(layer.polylines, hasLength(2));

    // Feature order == input geo order: [0] is the 2026-06-01 activity
    // (selected day), [1] is the 2026-06-02 activity (not selected).
    expect(layer.polylines[0].color, _kTrackColor);
    expect(layer.polylines[1].color, _kTrackColor.withAlpha(0x60));
  });
}
