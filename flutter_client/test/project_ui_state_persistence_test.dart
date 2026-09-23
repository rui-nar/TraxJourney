// Unit tests for ProjectNotifier's shared_preferences-backed UI-state
// persistence (issue #76 follow-up): a forced page reload (the black-screen
// JS backstop) wipes all in-memory Dart state, so selection + filters are
// round-tripped through shared_preferences, keyed per project, and restored
// on the next load() — but only when the restored reference still resolves
// to real data.

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'helpers/signed_in.dart';

/// Fixed project payload served by [_FakeProjectService]; load() reads
/// activities/items/day_meta from it, same shape as the real API.
class _FakeProjectService extends ProjectService {
  final Map<String, dynamic> details;
  _FakeProjectService(this.details);

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => details;

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async => details;
}

/// Skips owner-only network calls (sync-meta/share-info/background-sync
/// check) that `ProjectService` doesn't cover and that would otherwise hit
/// real HTTP in this test sandbox — irrelevant to the persistence logic under
/// test here, and the background-sync-check schedules a real 5 s Timer that
/// would leak past the end of a plain (non-widget) test.
class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service);

  @override
  bool get loadOwnerExtras => false;
}

Map<String, dynamic> _details({
  List<Map<String, dynamic>> activities = const [],
  List<Map<String, dynamic>> items = const [],
  Map<String, dynamic> dayMeta = const {},
}) =>
    {
      'name': 'Trip',
      'activities': activities,
      'items': items,
      'day_meta': dayMeta,
      'people': <dynamic>[],
      'groups': <dynamic>[],
      'sleeping_options': <dynamic>[],
    };

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    // UI state is saved per signed-in account; with no account there is
    // nothing to save it under, and the "not restored" cases below would pass
    // for the wrong reason.
    signInAs(3);
  });

  test(
      'selectDay persists and is restored by a fresh notifier loading the '
      'same project', () async {
    final details = _details(dayMeta: {'2026-05-10': <String, dynamic>{}});

    final n1 = _TestProjectNotifier(_FakeProjectService(details));
    await n1.load(const ProjectRef(name: 'Trip'));
    n1.selectDay('2026-05-10');
    // The persistence write is fire-and-forget (saveUiState() is void, per
    // the mixin's abstract hook signature) — give it a tick to land.
    await Future<void>.delayed(const Duration(milliseconds: 20));

    final n2 = _TestProjectNotifier(_FakeProjectService(details));
    await n2.load(const ProjectRef(name: 'Trip'));

    expect(n2.selectedDay, '2026-05-10');
  });

  test('a selectedActivityId for a since-deleted activity is not restored',
      () async {
    final activities = [
      {'id': 1, 'name': 'Ride', 'start_date_local': '2026-05-10T08:00:00'},
    ];
    final details = _details(activities: activities);

    final n1 = _TestProjectNotifier(_FakeProjectService(details));
    await n1.load(const ProjectRef(name: 'Trip'));
    n1.selectActivity(1);
    await Future<void>.delayed(const Duration(milliseconds: 20));

    // Second load: the activity no longer exists in the fresh data.
    final detailsAfterDelete = _details(activities: const []);
    final n2 = _TestProjectNotifier(_FakeProjectService(detailsAfterDelete));
    await n2.load(const ProjectRef(name: 'Trip'));

    expect(n2.selectedActivityId, isNull);
  });

  test('a selectedDay no longer present in day_meta is not restored',
      () async {
    final details = _details(dayMeta: {'2026-05-10': <String, dynamic>{}});

    final n1 = _TestProjectNotifier(_FakeProjectService(details));
    await n1.load(const ProjectRef(name: 'Trip'));
    n1.selectDay('2026-05-10');
    await Future<void>.delayed(const Duration(milliseconds: 20));

    final detailsNoDay = _details(dayMeta: const {});
    final n2 = _TestProjectNotifier(_FakeProjectService(detailsNoDay));
    await n2.load(const ProjectRef(name: 'Trip'));

    expect(n2.selectedDay, isNull);
  });

  test('filters persist and are restored by a fresh notifier', () async {
    final details = _details(dayMeta: {
      '2026-05-10': {'tags': ['norway']},
    });

    final n1 = _TestProjectNotifier(_FakeProjectService(details));
    await n1.load(const ProjectRef(name: 'Trip'));
    n1.setFilters(tags: {'norway'});
    await Future<void>.delayed(const Duration(milliseconds: 20));

    final n2 = _TestProjectNotifier(_FakeProjectService(details));
    await n2.load(const ProjectRef(name: 'Trip'));

    expect(n2.tagFilter, {'norway'});
  });
}
