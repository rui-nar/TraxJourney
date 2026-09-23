// Guards issue #123: choosing a person's Polarsteps trip looked like it did
// nothing. Two map-side causes, both covered here — view mode never rendered
// the overlay at all (only ManageMapPanel did), and neither map moved its
// camera, so a trip away from the project's own track was drawn off-screen.
//
// The `showEncounters` gate mirrors map_panel_encounters_test.dart: MapPanel is
// reused by the public share screen, and a person's trip is owner-only PII.

import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

ProjectNotifier _notifier() =>
    ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');

void _showTrip(ProjectNotifier n) {
  n.polarstepsOverlaySteps = [
    {'lat': 40.0, 'lon': -4.0},
    {'lat': 42.0, 'lon': -2.0},
  ];
  n.polarstepsOverlayLabel = 'Alice · Asia 2024';
  n.notifyListeners();
}

/// Mirrors view_screen.dart: MapPanel doesn't listen to the notifier itself,
/// its parent `Consumer` rebuilds it.
Widget _panel(ProjectNotifier notifier, AnimatedMapController controller,
        {bool? showEncounters}) =>
    MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: notifier,
          builder: (_, __) => MapPanel(
            notifier: notifier,
            mapController: controller,
            basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
            showEncounters: showEncounters ?? false,
          ),
        ),
      ),
    );

/// The fit animates from a post-frame callback — see map_panel_fit_bounds_test.
Future<void> _settleFit(WidgetTester tester) async {
  await tester.pump();
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 600));
}

void main() {
  setUp(TestWidgetsFlutterBinding.ensureInitialized);

  void sizeView(WidgetTester tester) {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
  }

  testWidgets('view mode renders the trip banner for the owner', (tester) async {
    sizeView(tester);
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);
    final notifier = _notifier();

    await tester.pumpWidget(_panel(notifier, controller, showEncounters: true));
    expect(find.text('Alice · Asia 2024'), findsNothing);

    _showTrip(notifier);
    await _settleFit(tester);

    expect(find.text('Alice · Asia 2024'), findsOneWidget);
    expect(find.byTooltip('Clear overlay'), findsOneWidget);
  });

  testWidgets('showEncounters omitted keeps the overlay off the shared map',
      (tester) async {
    sizeView(tester);
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);
    final notifier = _notifier();

    await tester.pumpWidget(_panel(notifier, controller));
    _showTrip(notifier);
    await _settleFit(tester);

    expect(find.text('Alice · Asia 2024'), findsNothing);
    expect(find.byTooltip('Clear overlay'), findsNothing);
    // The camera stays put too — nothing was drawn to move to.
    expect(controller.mapController.camera.center.latitude, closeTo(0, 0.001));
  });

  testWidgets('the camera moves to frame a newly-shown trip', (tester) async {
    sizeView(tester);
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);
    final notifier = _notifier();

    await tester.pumpWidget(_panel(notifier, controller, showEncounters: true));
    await _settleFit(tester);
    // Nothing shown yet: the camera sits on MapPanel's default centre.
    expect(controller.mapController.camera.center.latitude, closeTo(0, 0.001));

    _showTrip(notifier);
    await _settleFit(tester);

    final camera = controller.mapController.camera;
    expect(camera.center.latitude, closeTo(41.0, 0.5));
    expect(camera.center.longitude, closeTo(-3.0, 0.5));
    expect(camera.zoom, greaterThan(4));
  });

  testWidgets('clearing the overlay does not re-fit the camera', (tester) async {
    sizeView(tester);
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);
    final notifier = _notifier();

    await tester.pumpWidget(_panel(notifier, controller, showEncounters: true));
    _showTrip(notifier);
    await _settleFit(tester);
    final fitted = controller.mapController.camera.center;

    notifier.clearPolarstepsOverlay();
    await _settleFit(tester);

    expect(find.text('Alice · Asia 2024'), findsNothing);
    expect(controller.mapController.camera.center, fitted);
  });

  test('polarstepsOverlayPoints maps steps to LatLng in order', () {
    final points = polarstepsOverlayPoints([
      {'lat': 1, 'lon': 2},
      {'lat': 3.5, 'lon': 4.5},
    ]);
    expect(points, hasLength(2));
    expect(points.first.latitude, 1.0);
    expect(points.first.longitude, 2.0);
    expect(points.last.latitude, 3.5);
  });
}
