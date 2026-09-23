// Issue #299 — the map must not marshal polyline points on the UI isolate.
//
// _maybeDecimatePolylines used to convert every rendered point into a
// (double, double) record on the UI isolate and hand the result to
// compute(). Only a compute()'s *return* value is zero-copy; its argument is
// copied. On a real device that was 788 ms of marshalling plus roughly 1.6 s
// of argument copy — a 2.4 s stall inside build_specs which, because the
// camera-idle gate releases the geo swap after at most a couple of seconds,
// landed in the middle of an active pan and tripped Android's ANR watchdog.
//
// The decode hop now produces the decimated geometry, so the map looks it up
// instead. Asserted structurally: the marshalling span must never be recorded
// at all.

import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/heavy_decode.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _acts = 20;
// 50k points total — comfortably over the render budget, which issue #276
// raised to 40,000 so it stops binding below the server's own pixel-accurate
// output.
const _pts = 2500;

Map<String, dynamic> _rawGeo() => {
      'type': 'FeatureCollection',
      'features': [
        for (var a = 0; a < _acts; a++)
          {
            'type': 'Feature',
            'properties': {
              'type': 'activity',
              'activity_id': a,
              'sport_type': 'Ride',
            },
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                for (var i = 0; i < _pts; i++)
                  [7.0 + a * 0.05 + i * 1e-4, 45.0 + a * 0.05 + i * 1e-4]
              ],
            },
          },
      ],
    };

ProjectNotifier _notifier(Map<String, dynamic> geo) =>
    ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..geo = geo
      ..activities = [
        for (var a = 0; a < _acts; a++)
          {'id': a, 'start_date_local': '2026-06-01T08:00:00'}
      ]
      ..items = [
        for (var a = 0; a < _acts; a++)
          {'item_type': 'activity', 'activity_id': a}
      ]
      ..isLoading = false;

Future<void> _pumpMap(WidgetTester tester, ProjectNotifier n) async {
  tester.view.physicalSize = const Size(800, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final controller = AnimatedMapController(vsync: const TestVSync());
  addTearDown(controller.dispose);

  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: ListenableBuilder(
        listenable: n,
        builder: (_, __) => MapPanel(
          notifier: n,
          mapController: controller,
          basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
        ),
      ),
    ),
  ));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 600));
}

void main() {
  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    perfSpans
      ..reset()
      ..enabled = true;
  });

  tearDown(() {
    perfSpans
      ..reset()
      ..enabled = true; // the library default — see PerfSpans.enabled
  });

  testWidgets('geo from the decode hop is decimated without UI-isolate work',
      (tester) async {
    // Exactly how a real load arrives: through decodeGeoOffIsolate, which
    // seeds the geometry caches. runAsync because testWidgets' fake clock
    // never resolves a real isolate's future.
    final geo = await tester.runAsync(() => decodeGeoOffIsolate(
        Uint8List.fromList(utf8.encode(jsonEncode(_rawGeo())))));

    await _pumpMap(tester, _notifier(geo!));

    expect(perfSpans.blockingSpans['decimate_marshal'], isNull,
        reason: 'the UI isolate must not marshal points the worker already '
            'decimated');

    final layer = tester.widget<PolylineLayer>(find.byType(PolylineLayer).first);
    expect(layer.polylines, hasLength(_acts));
    var rendered = 0;
    for (final p in layer.polylines) {
      rendered += p.points.length;
    }
    expect(rendered, lessThanOrEqualTo(kMaxTotalPolylinePoints + _acts),
        reason: 'the render budget must actually be applied');
    expect(rendered, lessThan(_acts * _pts),
        reason: 'and it must really have reduced something');
  });

  testWidgets('geo the worker never saw still falls back to the compute path',
      (tester) async {
    // Client-built geo (E2EE trips) and locally patched segments never pass
    // through the decode hop, so the fallback has to stay working. Only the
    // synchronous marshalling is asserted here — the compute() it feeds
    // cannot complete under testWidgets' fake clock, and heavy_decode_test
    // already covers the decimation result itself.
    await _pumpMap(tester, _notifier(_rawGeo()));

    expect(perfSpans.blockingSpans['decimate_marshal'], hasLength(1),
        reason: 'unseeded geometry must still be decimated, off the UI '
            'isolate, via the original path');
  });
}
