// Widget tests for ProjectStatsScreen sourcing tags/groups from the ambient
// ProjectNotifier provider instead of GoRouterState.extra (issue #76
// follow-up: extra isn't URL-encoded, so it's lost on a forced reload), and
// loading the project into that notifier first when it isn't already the one
// loaded (e.g. a direct reload of the /stats URL, where the singleton starts
// empty).

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/project_stats_screen.dart';

class _FakeProjectService extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': <dynamic>[],
        'day_meta': {
          '2026-05-10': {
            'tags': ['norway']
          },
        },
        'people': <dynamic>[],
        'groups': <dynamic>[],
        'sleeping_options': <dynamic>[],
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  // load() unconditionally kicks off a background elevation-data fetch via
  // getDetails() (not gated by loadOwnerExtras) — override it too so nothing
  // falls through to a real, unmocked api.get() call.
  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

/// Skips owner-only network calls and the 5 s background-sync-check Timer
/// that would otherwise leak past testWidgets teardown — irrelevant here.
class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service);
  @override
  bool get loadOwnerExtras => false;
}

class _FakeStatsService extends ProjectService {
  @override
  Future<Map<String, dynamic>> getStats(ProjectRef ref,
          {List<String> tags = const []}) async =>
      <String, dynamic>{};
}

/// Stats payload with a mix of tagged and untagged ride distance, for the
/// "Distance by tag" breakdown (issue #203 follow-up).
class _FakeStatsServiceWithTagBreakdown extends ProjectService {
  @override
  Future<Map<String, dynamic>> getStats(ProjectRef ref,
          {List<String> tags = const []}) async =>
      <String, dynamic>{
        'distance_per_tag': {'Alps': 50000.0, 'Untagged': 30000.0},
      };
}

/// Records every `tags` argument getStats is called with, so a test can
/// assert what a filter-chip tap actually requested.
class _RecordingStatsService extends ProjectService {
  final List<List<String>> calls = [];

  @override
  Future<Map<String, dynamic>> getStats(ProjectRef ref,
      {List<String> tags = const []}) async {
    calls.add(tags);
    return <String, dynamic>{
      'tag_options': ['norway', 'Untagged'],
    };
  }
}

void main() {
  // ProjectNotifier.load() now round-trips selection/filter state through
  // shared_preferences (issue #76 follow-up) — without a mock, getInstance()
  // hangs on the platform channel in a widget test.
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets(
      'reads tags from the ambient ProjectNotifier, not a constructor param',
      (tester) async {
    final notifier = _TestProjectNotifier(_FakeProjectService());
    await notifier.load(const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(MaterialApp(
      home: ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: ProjectStatsScreen(
          projectName: 'Trip',
          service: _FakeStatsService(),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.widgetWithText(FilterChip, 'norway'), findsOneWidget);
  });

  testWidgets(
      'loads the project into the ambient notifier when it is not already loaded',
      (tester) async {
    final notifier = _TestProjectNotifier(_FakeProjectService());
    expect(notifier.projectName, isNull);

    await tester.pumpWidget(MaterialApp(
      home: ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: ProjectStatsScreen(
          projectName: 'Trip',
          service: _FakeStatsService(),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    expect(notifier.projectName, 'Trip');
  });

  testWidgets(
      'a ride day with no tags shows up as "Untagged" in the tag breakdown '
      '(issue #203 follow-up)', (tester) async {
    final notifier = _TestProjectNotifier(_FakeProjectService());
    await notifier.load(const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(MaterialApp(
      home: ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: ProjectStatsScreen(
          projectName: 'Trip',
          service: _FakeStatsServiceWithTagBreakdown(),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.textContaining('Untagged'), findsOneWidget);
    expect(find.textContaining('Alps'), findsOneWidget);
  });

  testWidgets(
      'the "Untagged" filter chip is selectable and requests untagged-only '
      'stats (issue #203 follow-up)', (tester) async {
    final notifier = _TestProjectNotifier(_FakeProjectService());
    await notifier.load(const ProjectRef(name: 'Trip'));
    final service = _RecordingStatsService();

    await tester.pumpWidget(MaterialApp(
      home: ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: ProjectStatsScreen(
          projectName: 'Trip',
          service: service,
        ),
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.widgetWithText(FilterChip, 'Untagged'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilterChip, 'Untagged'));
    await tester.pumpAndSettle();

    expect(service.calls.last, ['Untagged']);
  });
}
