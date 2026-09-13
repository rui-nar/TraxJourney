import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

// Must match `_kHereMarkerColor` in map_panel.dart.
const _kHereMarkerColor = Color(0xFF2563EB);

ProjectNotifier _notifier() =>
    ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');

Future<void> _pump(WidgetTester tester, Widget panel) async {
  tester.view.physicalSize = const Size(800, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(MaterialApp(home: Scaffold(body: panel)));
  await tester.pump();
}

bool _isHereMarker(Widget w) =>
    w is Container && (w.decoration as BoxDecoration?)?.color == _kHereMarkerColor;

/// Guards issue #88 (locate-me button on the map, edit + view modes) and,
/// mirroring map_panel_encounters_test.dart, the privacy regression it must
/// never introduce: `MapPanel` is reused by the public/unauthenticated share
/// screen (shared_project_screen.dart), which must never prompt anonymous
/// visitors for their device location.
void main() {
  group('MapPanel (view mode)', () {
    testWidgets(
        'showLocateMe:true renders the button and taps invoke onLocateMe',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);
      var taps = 0;

      await _pump(
        tester,
        MapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          showLocateMe: true,
          onLocateMe: () => taps++,
        ),
      );

      expect(find.byIcon(Icons.my_location), findsOneWidget);
      await tester.tap(find.byIcon(Icons.my_location));
      expect(taps, 1);
    });

    testWidgets(
        'showLocateMe omitted (default false) hides the button — the '
        'privacy guarantee for shared_project_screen.dart', (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);

      await _pump(
        tester,
        MapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          // showLocateMe intentionally omitted.
          onLocateMe: () {},
        ),
      );

      expect(find.byIcon(Icons.my_location), findsNothing);
    });

    testWidgets('locatingHere:true shows a busy spinner and disables the button',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);

      await _pump(
        tester,
        MapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          showLocateMe: true,
          locatingHere: true,
          onLocateMe: () {},
        ),
      );

      expect(find.byIcon(Icons.my_location), findsNothing);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      final button = tester.widget<IconButton>(find.byType(IconButton));
      expect(button.onPressed, isNull);
    });

    testWidgets('hereLatLng set renders the you-are-here marker',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);

      await _pump(
        tester,
        MapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          hereLatLng: const LatLng(45.0, 7.0),
        ),
      );

      expect(find.byWidgetPredicate(_isHereMarker), findsOneWidget);
    });

    testWidgets('hereLatLng omitted renders no you-are-here marker',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);

      await _pump(
        tester,
        MapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
        ),
      );

      expect(find.byWidgetPredicate(_isHereMarker), findsNothing);
    });
  });

  group('ManageMapPanel (edit mode)', () {
    testWidgets('onLocateMe set renders the button and taps invoke it',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);
      var taps = 0;

      await _pump(
        tester,
        ManageMapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          fittedNotifier: ValueNotifier(true),
          onLocateMe: () => taps++,
        ),
      );

      expect(find.byIcon(Icons.my_location), findsOneWidget);
      await tester.tap(find.byIcon(Icons.my_location));
      expect(taps, 1);
    });

    testWidgets('onLocateMe omitted hides the button', (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);

      await _pump(
        tester,
        ManageMapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          fittedNotifier: ValueNotifier(true),
          // onLocateMe intentionally omitted.
        ),
      );

      expect(find.byIcon(Icons.my_location), findsNothing);
    });

    testWidgets('hereLatLng set renders the you-are-here marker',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);

      await _pump(
        tester,
        ManageMapPanel(
          notifier: _notifier(),
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
          fittedNotifier: ValueNotifier(true),
          hereLatLng: const LatLng(45.0, 7.0),
        ),
      );

      expect(find.byWidgetPredicate(_isHereMarker), findsOneWidget);
    });
  });
}
