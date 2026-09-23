// Memory markers flashed and sometimes got stuck black: MarkerLayer keys its
// Positioned children by `Marker.key` (flutter_map 8.x), but no memory Marker
// ever set one. Without a stable key, any items-list rebuild (a real reload,
// the low-res→full-res geo upgrade, the post-import photo poller once fixed
// to actually notify) let Flutter's positional diffing tear down and recreate
// _MarkerThumbImageState — losing in-flight fetches and re-showing the black
// placeholder for a frame even on a cache hit. Fixed by keying each memory
// Marker (and its _MarkerThumbImage) by the memory id / thumb URL.
//
// This guards the key assignment itself, not the full rebuild/flash — that
// needs a running Flutter engine to observe frame-by-frame.

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

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

ProjectNotifier _notifier() => ProjectNotifier(ProjectService())
  ..ref = const ProjectRef(name: 'Trip')
  ..geo = const {'type': 'FeatureCollection', 'features': <dynamic>[]}
  ..items = [
    _memoryItem('mem-1', 47.0, 11.0),
    _memoryItem('mem-2', 48.0, 12.0),
  ]
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
  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
  });

  testWidgets('each memory marker carries a stable key derived from its id',
      (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await tester.pumpWidget(_panel(_notifier(), controller));
    await tester.pump();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    final memoryMarkers = tester
        .widgetList<MarkerLayer>(find.byType(MarkerLayer))
        .map((l) => l.markers)
        .firstWhere((markers) => markers.length == 2);

    final keys = memoryMarkers.map((m) => m.key).toList();
    expect(keys, [const ValueKey('memory-mem-1'), const ValueKey('memory-mem-2')]);
  });
}
