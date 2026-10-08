// Ghost trips stayed on screen after zooming out and back in: frozen copies of
// every keyed marker, at the positions they had while zoomed out.
//
// Once the world is narrower than the viewport, flutter_map's MarkerLayer draws
// one Positioned per visible world copy of each marker, and gives every copy
// the marker's own `key`. Our markers are keyed (memory, activity, segment,
// journal, encounter — see map_panel_memory_marker_key_test.dart for why), so
// one Stack ends up with sibling duplicate keys. Debug builds assert; release
// builds lose track of one copy, which is never removed and stays painted at
// its last position.
//
// The debug assertion is what this test observes: zoom out until the world
// repeats, and no duplicate-key error may be raised.

import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

Map<String, dynamic> _memoryItem(String id, double lat, double lon) => {
      'item_type': 'memory',
      'memory': {
        'id': id,
        'lat': lat,
        'lon': lon,
        'date': '2026-06-01',
        'photos': <String>[],
      },
    };

ProjectNotifier _notifier({
  List<Map<String, dynamic>>? items,
}) =>
    ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..geoFacetWriter.replaceKeepingLod(
          const {'type': 'FeatureCollection', 'features': <dynamic>[]})
      ..itemsFacetWriter.setItems(items ??
          [
            _memoryItem('mem-1', 60.0, 10.0),
            _memoryItem('mem-2', 61.0, 11.0),
          ])
      ..isLoading = false;

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
  testWidgets('zooming out until the world repeats raises no duplicate keys',
      (tester) async {
    // 1200 px wide: at zoom 0 the world is 256 px, so it repeats ~4 times.
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(_panel(_notifier(), controller));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    // Zoom out and back in, the gesture that left the ghosts behind.
    for (final zoom in [4.0, 2.0, 1.0, 0.0, 1.0, 4.0]) {
      controller.mapController.move(const LatLng(60, 10), zoom);
      await tester.pump();
      expect(tester.takeException(), isNull, reason: 'at zoom $zoom');
    }
  });

  testWidgets('world copies of a keyed marker are drawn, each keyed apart',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(_panel(_notifier(), controller));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));
    controller.mapController.move(const LatLng(60, 10), 0);
    await tester.pump();

    // The main copy keeps the plain key (thumbnail state survives rebuilds);
    // the copies exist, so the test above is not passing vacuously.
    const plain = ValueKey('memory-mem-1');
    expect(find.byKey(plain), findsOneWidget);
    final copies = find.byWidgetPredicate((w) {
      final key = w.key;
      return key is ValueKey<(Key, int)> && key.value.$1 == plain;
    });
    expect(copies, findsAtLeastNWidgets(1));
  });

  // Review R1-1: keying by world index alone remounted the marker (and
  // flashed its thumbnail) whenever the visible copy changed world, which a
  // pan across 180° does — flutter_map renormalises the camera centre there.
  testWidgets('panning across the antimeridian keeps the marker mounted',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(
        _panel(_notifier(items: [_memoryItem('fiji', -17.0, 179.5)]),
            controller));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    const plain = ValueKey('memory-fiji');
    // East of the line: the marker is in the main world.
    controller.mapController.move(const LatLng(-17, 179), 6);
    await tester.pump();
    expect(find.byKey(plain), findsOneWidget);
    final before = tester.element(find.byKey(plain));

    // West of the line: the same marker, now drawn as world copy -1.
    controller.mapController.move(const LatLng(-17, -179.5), 6);
    await tester.pump();
    expect(find.byKey(plain), findsOneWidget);
    expect(tester.element(find.byKey(plain)), same(before));
  });

  // Review R2-1: handing the plain key to the main copy whenever it was in
  // view remounted the copy already on screen as soon as the main one slid in.
  testWidgets('a second copy sliding into view leaves the watched one mounted',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(_panel(
        _notifier(items: [_memoryItem('far', 0, 150)]), controller));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    // Zoom 2: the world is 1024 px in a 1200 px view. Centred on -100°, the
    // marker at 150° is 250° east, off the right edge, so only its western
    // copy (world -1) is on screen, left of centre.
    const plain = ValueKey('memory-far');
    controller.mapController.move(const LatLng(0, -100), 2);
    await tester.pump();
    expect(find.byKey(plain), findsOneWidget);
    expect(tester.widget<Positioned>(find.byKey(plain)).left, lessThan(600));
    final watched = tester.element(find.byKey(plain));

    // Pan east: the main copy enters at the right edge, the watched one stays.
    controller.mapController.move(const LatLng(0, -50), 2);
    await tester.pump();
    final copies = find.byWidgetPredicate((w) {
      final key = w.key;
      return key is ValueKey<(Key, int)> && key.value.$1 == plain;
    });
    expect(copies, findsOneWidget, reason: 'the main copy is in view too');
    // The plain key, and its Element, are still on the left-hand copy.
    expect(tester.widget<Positioned>(find.byKey(plain)).left, lessThan(600));
    expect(tester.element(find.byKey(plain)), same(watched));
  });

  // Review R3-1: the copy beside the watched one was keyed by its world
  // index, which the camera's wrap across the antimeridian renumbers.
  testWidgets('both copies on screen stay mounted across the antimeridian',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(
        _panel(_notifier(items: [_memoryItem('near', 0, 2)]), controller));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    const plain = ValueKey('memory-near');
    final other = find.byWidgetPredicate((w) {
      final key = w.key;
      return key is ValueKey<(Key, int)> && key.value.$1 == plain;
    });

    // Zoom 2 (world 1024 px, view 1200 px), centred on 178°: the marker at 2°
    // is 176° west, near the left edge, and its next copy east is in view too.
    controller.mapController.move(const LatLng(0, 178), 2);
    await tester.pump();
    expect(find.byKey(plain), findsOneWidget);
    expect(other, findsOneWidget);
    final plainBefore = tester.element(find.byKey(plain));
    final otherBefore = tester.element(other);

    // Across the line: the camera centre wraps to -178°, renumbering worlds.
    controller.mapController.move(const LatLng(0, -178), 2);
    await tester.pump();
    expect(tester.element(find.byKey(plain)), same(plainBefore));
    expect(other, findsOneWidget);
    expect(tester.element(other), same(otherBefore));
  });
}
