// Switching auto-zoom on fits the current selection (#478), in both map panels.
//
// Before the fix only a selection CHANGE queued the fit, so turning the toggle
// on with a day already selected left the camera where it was.

import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Two activities on different days, far apart: Innsbruck (day 1) and Lisbon
/// (day 2). The whole-trip centre is nowhere near either.
Map<String, dynamic> _geo() => {
      'type': 'FeatureCollection',
      'features': [
        for (final a in [
          ['1', 11.35, 47.25, 11.55, 47.35],
          ['2', -9.20, 38.70, -9.10, 38.75],
        ])
          {
            'type': 'Feature',
            'properties': {'type': 'activity', 'activity_id': a[0]},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [a[1], a[2]],
                [a[3], a[4]],
              ],
            },
          },
      ],
    };

ProjectNotifier _notifier() => ProjectNotifier(ProjectService())
  ..ref = const ProjectRef(name: 'Trip')
  ..geoFacetWriter.replaceKeepingLod(_geo())
  ..activities = [
    {'id': '1', 'start_date_local': '2026-05-01T08:00:00'},
    {'id': '2', 'start_date_local': '2026-05-02T08:00:00'},
  ]
  ..items = [
    {'item_type': 'activity', 'activity_id': '1'},
    {'item_type': 'activity', 'activity_id': '2'},
  ]
  ..isLoading = false;

const _basemap = 'https://example.invalid/{z}/{x}/{y}.png';

typedef _Build = Widget Function(
    ProjectNotifier n, AnimatedMapController c, bool autoZoom);

Widget _viewPanel(ProjectNotifier n, AnimatedMapController c, bool autoZoom) =>
    MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: n,
          builder: (_, __) => MapPanel(
            notifier: n,
            mapController: c,
            basemapUrl: _basemap,
            autoZoom: autoZoom,
            // Carried-over viewport: skips the whole-trip fit, so only the
            // selection fit can move the camera.
            initialLat: 20.0,
            initialLng: 30.0,
            initialZoom: 3.0,
          ),
        ),
      ),
    );

Widget _managePanel(
        ProjectNotifier n, AnimatedMapController c, bool autoZoom) =>
    MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: n,
          builder: (_, __) => ManageMapPanel(
            notifier: n,
            mapController: c,
            basemapUrl: _basemap,
            autoZoom: autoZoom,
            fittedNotifier: ValueNotifier<bool>(true),
            initialLat: 20.0,
            initialLng: 30.0,
            initialZoom: 3.0,
          ),
        ),
      ),
    );

Future<void> _settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 800));
}

void _bigView(WidgetTester tester) {
  tester.view.physicalSize = const Size(800, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

void _suite(String name, _Build build) {
  group(name, () {
    testWidgets('toggling on with a selected day fits to it', (tester) async {
      _bigView(tester);
      final c = AnimatedMapController(vsync: const TestVSync());
      addTearDown(c.dispose);
      final n = _notifier();

      await tester.pumpWidget(build(n, c, false));
      await _settle(tester);
      n.selectedDays = {'2026-05-02'};
      n.notifyListeners();
      await _settle(tester);
      // Auto-zoom is off: selecting does not move the camera.
      expect(c.mapController.camera.center.latitude, closeTo(20.0, 0.001));

      await tester.pumpWidget(build(n, c, true));
      await _settle(tester);

      final camera = c.mapController.camera;
      expect(camera.center.latitude, closeTo(38.725, 0.2));
      expect(camera.center.longitude, closeTo(-9.15, 0.2));
      expect(camera.zoom, greaterThan(6));
    });

    testWidgets('toggling on with nothing selected does not move the camera',
        (tester) async {
      _bigView(tester);
      final c = AnimatedMapController(vsync: const TestVSync());
      addTearDown(c.dispose);
      final n = _notifier();

      await tester.pumpWidget(build(n, c, false));
      await _settle(tester);
      await tester.pumpWidget(build(n, c, true));
      await _settle(tester);

      final camera = c.mapController.camera;
      expect(camera.center.latitude, closeTo(20.0, 0.001));
      expect(camera.center.longitude, closeTo(30.0, 0.001));
      expect(camera.zoom, closeTo(3.0, 0.001));
    });

    testWidgets('toggling off does nothing', (tester) async {
      _bigView(tester);
      final c = AnimatedMapController(vsync: const TestVSync());
      addTearDown(c.dispose);
      final n = _notifier();

      await tester.pumpWidget(build(n, c, true));
      await _settle(tester);
      n.selectedDays = {'2026-05-02'};
      n.notifyListeners();
      await _settle(tester);
      final before = c.mapController.camera;

      // Move the camera away, then switch auto-zoom off: no fit happens.
      c.mapController.move(before.center, 4.0);
      await _settle(tester);
      await tester.pumpWidget(build(n, c, false));
      await _settle(tester);

      final camera = c.mapController.camera;
      expect(camera.center.latitude, closeTo(before.center.latitude, 0.001));
      expect(camera.zoom, closeTo(4.0, 0.001));
    });
  });
}

void main() {
  setUp(() => TestWidgetsFlutterBinding.ensureInitialized());
  _suite('MapPanel', _viewPanel);
  _suite('ManageMapPanel', _managePanel);
}
