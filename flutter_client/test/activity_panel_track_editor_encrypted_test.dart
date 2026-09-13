import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/projects/activity_editor_page.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Issue #29: the track editor needs a Dart polyline re-encoder + server-side
/// metric recompute that don't exist yet for ciphertext, so the "Edit track"
/// entry point must be disabled (with an explanatory message, mirroring the
/// "encrypted memory" translation-unavailable tone from issue #27) whenever
/// the activity's map.summary_polyline is an encrypted envelope — instead of
/// opening an editor that can't save.

/// Minimal Google-polyline encoder for a plaintext-track fixture.
String _encode(List<(double, double)> points) {
  final sb = StringBuffer();
  int prevLat = 0, prevLon = 0;
  for (final p in points) {
    final lat = (p.$1 * 1e5).round();
    final lon = (p.$2 * 1e5).round();
    _encodeValue(sb, lat - prevLat);
    _encodeValue(sb, lon - prevLon);
    prevLat = lat;
    prevLon = lon;
  }
  return sb.toString();
}

void _encodeValue(StringBuffer sb, int value) {
  int v = value < 0 ? ~(value << 1) : (value << 1);
  while (v >= 0x20) {
    sb.writeCharCode((0x20 | (v & 0x1f)) + 63);
    v >>= 5;
  }
  sb.writeCharCode(v + 63);
}

ProjectNotifier _notifierWithOneActivity(Map<String, dynamic> mapField) {
  final n = ProjectNotifier(ProjectService());
  n.activities = [
    {
      'id': 42,
      'type': 'Ride',
      'name': 'Ride',
      'distance': 5000,
      'moving_time': 1800,
      'start_date_local': '2026-06-01T08:00:00',
      'map': mapField,
      'elevation_profile': [
        [0.0, 10.0],
        [1.0, 20.0],
      ],
    },
  ];
  n.items = [
    {'item_type': 'activity', 'activity_id': 42},
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
  // Days start collapsed; expand so the activity row (and its edit icon) renders.
  await tester.tap(find.byIcon(Icons.unfold_more));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
      'tapping "Edit track" on an encrypted activity shows a message and does '
      'NOT open the editor', (tester) async {
    final notifier =
        _notifierWithOneActivity({'summary_polyline': 'v1.d2VsY29tZQ==.Y2lwaGVy'});
    await _pumpPanel(tester, notifier);

    await tester.tap(find.byTooltip('Edit track'));
    await tester.pump(); // let the SnackBar animation start

    expect(find.byType(ActivityEditorPage), findsNothing);
    expect(
      find.text("This activity is encrypted and its track can't be edited."),
      findsOneWidget,
    );
  });

  testWidgets('tapping "Edit track" on a plaintext activity opens the editor',
      (tester) async {
    final line = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03)];
    final notifier = _notifierWithOneActivity({'summary_polyline': _encode(line)});
    await _pumpPanel(tester, notifier);

    await tester.tap(find.byTooltip('Edit track'));
    await tester.pumpAndSettle();

    expect(find.byType(ActivityEditorPage), findsOneWidget);
  });
}
