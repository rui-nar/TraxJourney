// Widget tests for FramePickerOverlay's bottom control bar (issue #14, unit
// F; paper-size selector added alongside the orientation toggle). Mirrors
// map_panel_locate_me_test.dart's pattern of pumping a real FlutterMap with
// an AnimatedMapController(vsync: const TestVSync()) so the controller is
// actually attached and `_confirm`'s `camera.visibleBounds` read succeeds.

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';

import 'package:traxjourney_client/src/projects/map_panel.dart';

Future<void> _pump(WidgetTester tester, Widget overlay,
    AnimatedMapController controller) async {
  tester.view.physicalSize = const Size(800, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Stack(
        children: [
          FlutterMap(
            mapController: controller.mapController,
            options: const MapOptions(
              initialCenter: LatLng(45.0, 7.0),
              initialZoom: 10,
            ),
            children: const [],
          ),
          overlay,
        ],
      ),
    ),
  ));
  await tester.pump();
}

void main() {
  group('FramePickerOverlay', () {
    testWidgets('defaults to A0 and reports it on Next', (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);
      String? reportedPaperSize;

      await _pump(
        tester,
        FramePickerOverlay(
          mapController: controller,
          onNext: (bounds, orientation, paperSize) =>
              reportedPaperSize = paperSize,
          onCancel: () {},
        ),
        controller,
      );

      expect(find.text('A0'), findsOneWidget);
      await tester.tap(find.text('Next'));
      expect(reportedPaperSize, 'A0');
    });

    testWidgets('selecting a different paper size reports it on Next',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);
      String? reportedPaperSize;
      LatLngBounds? reportedBounds;
      String? reportedOrientation;

      await _pump(
        tester,
        FramePickerOverlay(
          mapController: controller,
          onNext: (bounds, orientation, paperSize) {
            reportedBounds = bounds;
            reportedOrientation = orientation;
            reportedPaperSize = paperSize;
          },
          onCancel: () {},
        ),
        controller,
      );

      await tester.tap(find.byType(DropdownButton<String>));
      await tester.pumpAndSettle();
      // Two 'A3' texts now exist (the closed dropdown's selected value plus
      // the open menu's item) — pick the menu item.
      await tester.tap(find.text('A3').last);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Next'));
      expect(reportedPaperSize, 'A3');
      // Orientation and bounds still travel alongside the new paper size.
      expect(reportedOrientation, 'landscape');
      expect(reportedBounds, isNotNull);
    });

    testWidgets('paper size selection survives an orientation toggle',
        (tester) async {
      final controller = AnimatedMapController(vsync: const TestVSync());
      addTearDown(controller.dispose);
      String? reportedPaperSize;

      await _pump(
        tester,
        FramePickerOverlay(
          mapController: controller,
          onNext: (bounds, orientation, paperSize) =>
              reportedPaperSize = paperSize,
          onCancel: () {},
        ),
        controller,
      );

      await tester.tap(find.byType(DropdownButton<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('A2').last);
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.crop_landscape));
      await tester.pump();

      await tester.tap(find.text('Next'));
      expect(reportedPaperSize, 'A2');
    });
  });
}
