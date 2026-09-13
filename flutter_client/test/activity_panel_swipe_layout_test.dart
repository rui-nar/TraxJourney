import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Regression for activity-panel scroll jank: list rows must only be wrapped in
/// a (heavyweight) Dismissible on narrow/touch layouts. On wide/desktop the
/// edit/delete actions live in each row's trailing buttons, so the per-row
/// Dismissible is removed to keep scrolling smooth.
void main() {
  ProjectNotifier notifierWithOneActivity() {
    final n = ProjectNotifier(ProjectService());
    n.activities = [
      {
        'id': 1,
        'type': 'Run',
        'name': 'Morning Run',
        'distance': 5000,
        'moving_time': 1800,
        'start_date_local': '2026-06-01T08:00:00',
      },
    ];
    n.items = [
      {'item_type': 'activity', 'activity_id': 1},
    ];
    return n;
  }

  Future<void> pumpPanel(WidgetTester tester, Size size) async {
    tester.view.physicalSize = size;
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final notifier = notifierWithOneActivity();
    await tester.pumpWidget(
      ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: MaterialApp(
          home: Scaffold(body: ActivityPanel(notifier: notifier)),
        ),
      ),
    );
    // Days start collapsed; expand so the activity row renders.
    await tester.tap(find.byIcon(Icons.unfold_more));
    await tester.pumpAndSettle();
  }

  testWidgets('wide layout: rows are NOT wrapped in Dismissible', (tester) async {
    await pumpPanel(tester, const Size(1200, 900));

    final row = find.byKey(const ValueKey('act_1'));
    expect(row, findsOneWidget);
    expect(tester.widget(row), isA<KeyedSubtree>());
    // Sanity: no item-row Dismissible on wide.
    expect(find.byType(Dismissible), findsNothing);
  });

  testWidgets('narrow layout: rows ARE wrapped in Dismissible', (tester) async {
    await pumpPanel(tester, const Size(400, 900));

    final row = find.byKey(const ValueKey('act_1'));
    expect(row, findsOneWidget);
    expect(tester.widget(row), isA<Dismissible>());
  });
}
