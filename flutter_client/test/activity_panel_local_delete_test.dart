import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Issue #37: a split-tail (local, negative-id) activity gets a dedicated
/// "delete local activity" affordance in its row. Normal (positive-id)
/// activities must NOT show it.
void main() {
  ProjectNotifier notifierWith({required int activityId, String name = 'Ride'}) {
    final n = ProjectNotifier(ProjectService());
    n.activities = [
      {
        'id': activityId,
        'type': 'Ride',
        'name': name,
        'distance': 5000,
        'moving_time': 1800,
        'start_date_local': '2026-06-01T08:00:00',
        'manual': activityId < 0,
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

  testWidgets('local (negative-id) activity shows the delete-local affordance',
      (tester) async {
    final notifier = notifierWith(activityId: -7);
    await pumpPanel(tester, notifier);

    expect(find.byKey(const ValueKey('del_local_-7')), findsOneWidget);
    expect(find.byIcon(Icons.delete_forever), findsOneWidget);
  });

  testWidgets(
      'local (negative-id) activity shows exactly one delete icon (#120)',
      (tester) async {
    final notifier = notifierWith(activityId: -7);
    await pumpPanel(tester, notifier);

    // Regression: it must NOT also show the generic delete affordance
    // alongside the dedicated one.
    expect(find.byIcon(Icons.delete_outline), findsNothing);
    expect(find.byIcon(Icons.delete_forever), findsOneWidget);
  });

  testWidgets('normal (positive-id) activity does NOT show it', (tester) async {
    final notifier = notifierWith(activityId: 42);
    await pumpPanel(tester, notifier);

    expect(find.byKey(const ValueKey('del_local_42')), findsNothing);
    expect(find.byIcon(Icons.delete_forever), findsNothing);
  });

  testWidgets('tapping it calls deleteLocalActivity via the service',
      (tester) async {
    final service = _RecordingService();
    final notifier = ProjectNotifier(service);
    notifier.ref = const ProjectRef(name: 'trip');
    notifier.activities = [
      {
        'id': -7,
        'type': 'Ride',
        'name': 'Split tail',
        'distance': 5000,
        'moving_time': 1800,
        'start_date_local': '2026-06-01T08:00:00',
        'manual': true,
      },
    ];
    notifier.items = [
      {'item_type': 'activity', 'activity_id': -7},
    ];
    await pumpPanel(tester, notifier);

    await tester.tap(find.byKey(const ValueKey('del_local_-7')));
    await tester.pump();

    // Optimistic removal happens immediately.
    expect(notifier.items, isEmpty);

    // The confirm (real delete) fires after the undo window elapses.
    await tester.pump(const Duration(milliseconds: 6000));
    expect(service.deletedLocal, [('trip', -7)]);
  });
}

/// Minimal ProjectService stub that records the local-delete call and
/// short-circuits the follow-up reload (getDetailsMeta + getGeo) so no HTTP
/// is attempted from the test.
class _RecordingService extends ProjectService {
  final List<(String, int)> deletedLocal = [];

  @override
  Future<void> deleteLocalActivity(ProjectRef ref, int activityId) async {
    deletedLocal.add((ref.name, activityId));
  }

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': const [],
        'items': const [],
      };

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async => {
        'type': 'FeatureCollection',
        'features': const [],
      };
}
