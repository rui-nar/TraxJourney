import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// GPX-import visual differentiation: an activity with source == 'gpx' shows
/// a small corner badge on its icon box; a Strava (source null/absent)
/// activity does not.
void main() {
  ProjectNotifier notifierWith({required int activityId, String? source}) {
    final n = ProjectNotifier(ProjectService());
    n.activities = [
      {
        'id': activityId,
        'type': 'Ride',
        'name': 'Ride',
        'distance': 5000,
        'moving_time': 1800,
        'start_date_local': '2026-06-01T08:00:00',
        if (source != null) 'source': source,
      },
    ];
    n.items = [
      {'item_type': 'activity', 'activity_id': activityId},
    ];
    return n;
  }

  Future<void> pumpPanel(WidgetTester tester, ProjectNotifier notifier) async {
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
    // Days start collapsed; expand so the activity row renders.
    await tester.tap(find.byIcon(Icons.unfold_more));
    await tester.pumpAndSettle();
  }

  testWidgets('Strava activity (no source) shows no GPX badge', (tester) async {
    final notifier = notifierWith(activityId: 1);
    await pumpPanel(tester, notifier);

    expect(find.byIcon(Icons.route), findsNothing);
  });

  testWidgets('GPX-imported activity (source == gpx) shows the corner badge',
      (tester) async {
    final notifier = notifierWith(activityId: 2, source: 'gpx');
    await pumpPanel(tester, notifier);

    expect(find.byIcon(Icons.route), findsOneWidget);
  });

  testWidgets('the badge says what it means, for a reader and a screen reader',
      (tester) async {
    // A 9 px glyph with no accessible name is decoration: a screen reader
    // announced nothing at all, and a sighted user had no way to find out what
    // it meant either (issue #260, unit 6).
    // Semantics only exist while a handle is held, and the handle has to be
    // released inside the test body: the framework checks for leaked handles
    // before tearDowns run.
    final semantics = tester.ensureSemantics();

    await pumpPanel(tester, notifierWith(activityId: -7, source: 'gpx'));

    expect(find.byKey(const ValueKey('gpx_source_badge')), findsOneWidget);
    // The row is a ListTile, which merges its children into one node, so the
    // announcement is the row's label with the badge's phrase inside it.
    expect(find.bySemanticsLabel(RegExp('Imported from a GPX file')),
        findsAtLeastNWidgets(1));
    expect(find.byTooltip('Imported from a GPX file'), findsOneWidget);

    semantics.dispose();
  });

  testWidgets('a synced activity says nothing about a source', (tester) async {
    final semantics = tester.ensureSemantics();

    await pumpPanel(tester, notifierWith(activityId: 4242));

    expect(find.byKey(const ValueKey('gpx_source_badge')), findsNothing);
    expect(find.bySemanticsLabel(RegExp('Imported from a GPX file')),
        findsNothing);

    semantics.dispose();
  });
}
