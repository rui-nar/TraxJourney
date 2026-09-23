import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Same activity as [_notifierWithActivity], plus a memory, a journal entry
/// and an owner-only encounter — one marker of each kind other than
/// Activities, so a toggle that only concerns Activities can be checked for
/// side effects on all of them.
ProjectNotifier _notifierWithMixedMarkers() {
  final n = ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');
  n.people = [
    {'id': 1, 'name': 'Alice'},
  ];
  n.items = [
    {
      'item_type': 'activity',
      'activity': {'id': 1, 'name': 'Hike', 'sport_type': 'hike'},
    },
    {
      'item_type': 'memory',
      'memory': {
        'id': 'mem-1',
        'lat': 45.05,
        'lon': 7.05,
        'date': '2026-01-01',
        'photos': <String>[],
      },
    },
    {
      'item_type': 'journal',
      'journal': {'id': 'jrnl-1', 'lat': 45.02, 'lon': 7.02},
    },
    {
      'item_type': 'encounter',
      'encounter': {
        'id': 10,
        'person_id': 1,
        'lat': 45.03,
        'lon': 7.03,
        'date': '2026-01-01',
      },
    },
  ];
  n.geo = {
    'type': 'FeatureCollection',
    'features': [
      {
        'type': 'Feature',
        'properties': {'type': 'activity', 'activity_id': '1', 'sport_type': 'hike'},
        'geometry': {
          'type': 'LineString',
          'coordinates': [
            [7.0, 45.0],
            [7.1, 45.1],
          ],
        },
      },
    ],
  };
  return n;
}

ProjectNotifier _notifierWithActivity() {
  final n = ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');
  n.items = [
    {
      'item_type': 'activity',
      'activity': {'id': 1, 'name': 'Hike', 'sport_type': 'hike'},
    },
  ];
  n.geo = {
    'type': 'FeatureCollection',
    'features': [
      {
        'type': 'Feature',
        'properties': {'type': 'activity', 'activity_id': '1', 'sport_type': 'hike'},
        'geometry': {
          'type': 'LineString',
          'coordinates': [
            [7.0, 45.0],
            [7.1, 45.1],
          ],
        },
      },
    ],
  };
  return n;
}

Future<void> _pump(WidgetTester tester, MapPanel panel) async {
  tester.view.physicalSize = const Size(800, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(MaterialApp(home: Scaffold(body: panel)));
  // The activity geo triggers _fitBoundsOnce's animated camera fit (a
  // post-frame callback): one pump to run the build + callback, a second so
  // the ticker takes its zero-elapsed first tick, then the animation's own
  // duration — otherwise its Ticker is still running when the test ends
  // (see map_panel_fit_bounds_test.dart's _settleFit).
  await tester.pump();
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 600));
}

/// Guards issue #215 (an Activities toggle, alongside Memories/Encounters,
/// to hide the activity icons on a crowded map).
void main() {
  testWidgets('activity markers render an Activities toggle that hides them',
      (tester) async {
    final notifier = _notifierWithActivity();
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await _pump(
      tester,
      MapPanel(
        notifier: notifier,
        mapController: controller,
        basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
      ),
    );

    expect(find.text('Activities'), findsOneWidget);
    expect(find.byIcon(Icons.hiking), findsOneWidget);

    await tester.tap(find.byType(Switch).first);
    await tester.pump();

    expect(find.byIcon(Icons.hiking), findsNothing);
  });

  testWidgets('no activity markers hides the Activities toggle',
      (tester) async {
    final notifier = ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip');
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await _pump(
      tester,
      MapPanel(
        notifier: notifier,
        mapController: controller,
        basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
      ),
    );

    expect(find.text('Activities'), findsNothing);
  });

  // Regression test for the ANR fixed alongside this: the five MarkerLayers
  // in _MapPanelState.build() were unkeyed, so toggling one layer's presence
  // (here, hiding Activities) shifted every later layer's position in
  // FlutterMap.children. With no key to match on, Flutter's list
  // reconciliation reused each shifted-into-place MarkerLayer Element for a
  // *different* logical layer's marker list — and because that Element's
  // previous Positioned children were keyed (or not) differently from the
  // new ones, the whole subtree (including memory thumbnail widgets) was
  // torn down and rebuilt in one frame. On a real trip (219 activities, 158
  // memories) that synchronous rebuild was the ANR.
  //
  // This proves the fix by Element identity, not just "no exception was
  // thrown": every marker/layer Element below is captured by its stable key
  // before the toggle and must be the *same instance* afterwards, both when
  // Activities is hidden and when it's shown again.
  testWidgets(
      'toggling Activities off and back on does not remount memory/journal/'
      'encounter markers or layers', (tester) async {
    final notifier = _notifierWithMixedMarkers();
    final controller = AnimatedMapController(vsync: const TestVSync());
    addTearDown(controller.dispose);

    await _pump(
      tester,
      MapPanel(
        notifier: notifier,
        mapController: controller,
        basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
        showEncounters: true,
      ),
    );

    final keyedFinders = {
      'memories-layer': find.byKey(const ValueKey('memories-layer')),
      'journal-layer': find.byKey(const ValueKey('journal-layer')),
      'encounters-layer': find.byKey(const ValueKey('encounters-layer')),
      'memory-mem-1': find.byKey(const ValueKey('memory-mem-1')),
      'journal-jrnl-1': find.byKey(const ValueKey('journal-jrnl-1')),
      'encounter-10': find.byKey(const ValueKey('encounter-10')),
    };
    for (final entry in keyedFinders.entries) {
      expect(entry.value, findsOneWidget, reason: entry.key);
    }
    final before = {
      for (final entry in keyedFinders.entries)
        entry.key: tester.element(entry.value),
    };

    // Hide Activities (the first of the toggle switches).
    await tester.tap(find.byType(Switch).first);
    await tester.pump();
    expect(find.byIcon(Icons.hiking), findsNothing);

    for (final entry in keyedFinders.entries) {
      expect(tester.element(entry.value), same(before[entry.key]),
          reason: '${entry.key} was remounted when Activities was hidden');
    }

    // Show Activities again.
    await tester.tap(find.byType(Switch).first);
    await tester.pump();
    expect(find.byIcon(Icons.hiking), findsOneWidget);

    for (final entry in keyedFinders.entries) {
      expect(tester.element(entry.value), same(before[entry.key]),
          reason: '${entry.key} was remounted when Activities was shown again');
    }
  });
}
