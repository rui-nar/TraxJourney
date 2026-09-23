// A degraded (straight-line) route resolution is a success server-side
// (route_status="resolved", route_degraded=true — see api/segments.py), but
// before this the activity panel rendered it identically to a fully
// successful resolve: no indication anywhere that the geometry is only an
// approximate straight line rather than the real track (issue #213).

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

Map<String, dynamic> _segment({
  String id = 'seg-1',
  bool degraded = false,
  String? routeStatus = 'resolved',
}) {
  return {
    'id': id,
    'segment_type': 'boat',
    'label': 'Helsinki -> Tallinn',
    'date': '2026-01-01',
    'start': {'lat': 60.1719, 'lon': 24.9414},
    'end': {'lat': 59.4370, 'lon': 24.7536},
    'route_mode': 'ferry',
    'route_status': routeStatus,
    'route_degraded': degraded,
    'route_polyline': jsonEncode([
      [24.9414, 60.1719],
      [24.7536, 59.4370],
    ]),
    'route_edited': false,
  };
}

ProjectNotifier _notifierWith(Map<String, dynamic> segment) {
  final n = ProjectNotifier(ProjectService());
  n.items = [
    {'item_type': 'segment', 'segment': segment},
  ];
  return n;
}

Future<void> _pumpPanel(WidgetTester tester, ProjectNotifier notifier) async {
  tester.view.physicalSize = const Size(1200, 900);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(
    ChangeNotifierProvider<ProjectNotifier>.value(
      value: notifier,
      child: MaterialApp(
        home: Scaffold(body: ActivityPanel(notifier: notifier)),
      ),
    ),
  );
  // Days start collapsed; expand so the segment row renders.
  await tester.tap(find.byIcon(Icons.unfold_more));
  // Not pumpAndSettle(): a 'pending' segment renders an indeterminate
  // CircularProgressIndicator, whose animation never settles and hangs this
  // forever. Two pumps (build + the tap's own frame) is enough either way.
  await tester.pump();
  await tester.pump();
}

void main() {
  testWidgets('a degraded resolve shows an approximate-route indicator',
      (tester) async {
    final notifier = _notifierWith(_segment(degraded: true));
    await _pumpPanel(tester, notifier);

    expect(find.text('Approximate route — tap to retry'), findsOneWidget);
    expect(find.byIcon(Icons.timeline), findsOneWidget);
  });

  testWidgets('a full (non-degraded) resolve shows no status line',
      (tester) async {
    final notifier = _notifierWith(_segment(degraded: false));
    await _pumpPanel(tester, notifier);

    expect(find.text('Approximate route — tap to retry'), findsNothing);
    expect(find.text('Resolving route…'), findsNothing);
    expect(find.text('Route failed — tap to retry'), findsNothing);
  });

  testWidgets('a pending resolve still shows the plain spinner, not the '
      'degraded indicator', (tester) async {
    final notifier = _notifierWith(
        _segment(routeStatus: 'pending', degraded: false));
    await _pumpPanel(tester, notifier);

    expect(find.text('Resolving route…'), findsOneWidget);
    expect(find.text('Approximate route — tap to retry'), findsNothing);
  });
}
