// Guards the view-mode "zoom to frame the trip on open" animation.
//
// Regression: view mode mounts MapPanel as soon as low-res geo lands, but
// `_fitBoundsOnce` waits for `isLoading` to go false (meta still in flight).
// In that window the debounced camera→URL sync wrote lat/lng/zoom onto the
// route, MapPanel was rebuilt with a non-null `initialLat`, and the fit flag —
// evaluated lazily on first use — read that self-written param as "the user
// arrived with an explicit viewport" and skipped the fit entirely.

import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// A one-activity track around Innsbruck — far from the (0, 0) default centre,
/// so "did we fit?" is unambiguous.
Map<String, dynamic> _geo() => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': '1'},
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [11.35, 47.25],
              [11.45, 47.30],
              [11.55, 47.35],
            ],
          },
        },
      ],
    };

ProjectNotifier _notifier({required bool loading}) =>
    ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..geo = _geo()
      ..isLoading = loading;

/// Mirrors view_screen.dart: MapPanel doesn't listen to the notifier itself,
/// its parent `Consumer` rebuilds it.
Widget _panel(ProjectNotifier notifier, AnimatedMapController controller,
        {double? initialLat, double? initialLng, double? initialZoom}) =>
    MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: notifier,
          builder: (_, __) => MapPanel(
            notifier: notifier,
            mapController: controller,
            basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
            initialLat: initialLat,
            initialLng: initialLng,
            initialZoom: initialZoom,
          ),
        ),
      ),
    );

Future<void> _settleFit(WidgetTester tester) async {
  // _fitBoundsOnce animates from a post-frame callback: one pump to run the
  // build + callback, a second so the ticker takes its zero-elapsed first
  // tick, then the animation's own duration.
  await tester.pump();
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 600));
}

void main() {
  setUp(() {
    // Wide enough that the fit has room to zoom in past the initial zoom.
    TestWidgetsFlutterBinding.ensureInitialized();
  });

  testWidgets('fits the whole trip once loading finishes', (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);
    final notifier = _notifier(loading: true);

    await tester.pumpWidget(_panel(notifier, controller));
    await _settleFit(tester);
    // Still loading → no fit yet; camera sits on MapPanel's default centre.
    expect(controller.mapController.camera.center.latitude, closeTo(0, 0.001));

    notifier.isLoading = false;
    notifier.notifyListeners();
    await _settleFit(tester);

    final camera = controller.mapController.camera;
    expect(camera.center.latitude, closeTo(47.30, 0.2));
    expect(camera.center.longitude, closeTo(11.45, 0.2));
    expect(camera.zoom, greaterThan(6));
  });

  testWidgets(
      'still fits when the camera→URL sync adds lat/lng mid-load '
      '(regression: view mode stopped zooming to frame the trip)',
      (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);
    final notifier = _notifier(loading: true);

    await tester.pumpWidget(_panel(notifier, controller));
    await _settleFit(tester);

    // The debounced sync fires while meta is still loading and rewrites the
    // route with the *default* camera — MapPanel is rebuilt with those params.
    await tester.pumpWidget(_panel(notifier, controller,
        initialLat: 0.0, initialLng: 0.0, initialZoom: 2.0));
    await _settleFit(tester);

    notifier.isLoading = false;
    notifier.notifyListeners();
    await _settleFit(tester);

    final camera = controller.mapController.camera;
    expect(camera.center.latitude, closeTo(47.30, 0.2));
    expect(camera.center.longitude, closeTo(11.45, 0.2));
    expect(camera.zoom, greaterThan(6));
  });

  testWidgets('an initial viewport supplied at mount suppresses the fit',
      (tester) async {
    // Mode toggle (view ⇄ edit) carries the camera over deliberately; that
    // must still win over fit-to-bounds.
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);
    final notifier = _notifier(loading: false);

    await tester.pumpWidget(_panel(notifier, controller,
        initialLat: 20.0, initialLng: 30.0, initialZoom: 5.0));
    await _settleFit(tester);

    final camera = controller.mapController.camera;
    expect(camera.center.latitude, closeTo(20.0, 0.001));
    expect(camera.center.longitude, closeTo(30.0, 0.001));
    expect(camera.zoom, closeTo(5.0, 0.001));
  });
}
