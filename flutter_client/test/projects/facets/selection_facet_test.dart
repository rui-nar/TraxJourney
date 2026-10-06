// The selection facet holds what the user has selected and filtered (issue
// #294, Decisions 17 and 19 of docs/CLIENT_STATE_MAP_PLAN.md): a selection
// change bumps only its version, each notifier call still notifies once, and
// clear() puts it all back.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import '../../helpers/signed_in.dart';

const _me = 3;
const _ref = ProjectRef(name: 'Trip');
const _key = 'project_ui_state_$_me:$_me:Trip';
const _day1 = '2026-06-01';
const _day2 = '2026-06-02';

class _Service extends ProjectService {
  Map<String, dynamic> _payload() => {
        'name': 'Trip',
        'lock_version': 1,
        'activities': [
          {
            'id': 1,
            'name': 'Morning ride',
            'type': 'Ride',
            'start_date_local': '${_day1}T09:00:00',
          },
        ],
        'items': [
          {'item_type': 'activity', 'activity_id': 1},
        ],
        'day_meta': {
          _day1: {'tags': ['beach']},
          _day2: {},
        },
        'people': <dynamic>[],
        'groups': <dynamic>[],
      };

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async =>
      _payload();

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _payload();

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};
}

Future<ProjectNotifier> _loaded() async {
  final n = ProjectNotifier(_Service())
    ..loadRetryBackoff = const [Duration(milliseconds: 1)];
  await n.load(_ref);
  await pumpEventQueue();
  return n;
}

void main() {
  setUp(() {
    signInAs(_me);
    SharedPreferences.setMockInitialValues({});
  });

  test('a selection change bumps only the selection version', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = (
      n.geoFacet.version,
      n.styleFacet.version,
      n.itemsFacet.version,
      n.elevationFacet.version,
      n.selectionFacet.version,
    );

    n.selectActivity(1);

    expect(n.selectionFacet.version, before.$5 + 1);
    expect(
        (
          n.geoFacet.version,
          n.styleFacet.version,
          n.itemsFacet.version,
          n.elevationFacet.version,
        ),
        (before.$1, before.$2, before.$3, before.$4));
    expect(n.selectionFacet.selectedActivityId, 1);
  });

  test('every selection setter notifies the facet once, not the root',
      () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    var root = 0, facet = 0;
    n.addListener(() => root++);
    n.selectionFacet.addListener(() => facet++);

    final calls = <void Function()>[
      () => n.selectActivity(1),
      () => n.selectSegment(2),
      () => n.selectMemory(3),
      () => n.selectJournal(4),
      () => n.toggleJournals(),
      () => n.selectDay(_day1),
      () => n.selectDays({_day1, _day2}),
    ];
    for (final call in calls) {
      call();
    }

    expect((root, facet), (0, calls.length));
  });

  test('selecting one kind clears the others, and the same id deselects', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final s = n.selectionFacet;

    n.selectDay(_day1);
    n.selectActivity(1);
    expect((s.selectedActivityId, s.selectedDay), (1, null));
    n.selectActivity('1');
    expect(s.selectedActivityId, isNull, reason: 'compared as strings');

    n.selectSegment(2);
    n.selectMemory(3);
    expect((s.selectedSegmentId, s.selectedMemoryId), (null, 3));
    n.selectJournal(4);
    expect((s.selectedMemoryId, s.selectedJournalId), (null, 4));

    n.selectDays({_day1});
    expect(s.selectedDays, {_day1});
    n.selectDay(_day2);
    expect(s.selectedDay, _day2);
    expect(s.selectedDays, isEmpty);

    expect(s.showJournals, isTrue);
    n.toggleJournals();
    expect(s.showJournals, isFalse);
  });

  test('filters narrow the days, drop the item selection and count', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final s = n.selectionFacet;
    n.selectActivity(1);

    n.setFilters(tags: {'beach'});

    expect(s.tagFilter, {'beach'});
    expect(s.activeFilterCount, 1);
    expect(s.hasActiveFilter, isTrue);
    expect(s.selectedDays, containsAll({_day1, _day2}),
        reason: 'day 2 inherits the tag');
    expect(s.selectedActivityId, isNull);

    n.clearAllFilters();
    expect(s.hasActiveFilter, isFalse);
    expect(s.selectedDays, isEmpty);
  });

  test('restoreSavedUiState re-reads the saved state and notifies once',
      () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    n.selectActivity(1);
    SharedPreferences.setMockInitialValues({
      _key: jsonEncode({
        'selectedDay': _day2,
        'tags': ['beach'],
      }),
    });
    var root = 0, facet = 0;
    n.addListener(() => root++);
    n.selectionFacet.addListener(() => facet++);

    await n.restoreSavedUiState();

    expect((root, facet), (0, 1));
    final s = n.selectionFacet;
    expect(s.selectedDay, _day2);
    expect(s.selectedActivityId, isNull,
        reason: 'what this notifier selected is cleared first');
    expect(s.tagFilter, {'beach'});
  });

  test('clear() puts the selection and the filters back', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    n.selectJournal(4);
    n.toggleJournals();
    n.setFilters(tags: {'beach'});
    n.selectDays({_day1});

    n.clear();

    final s = n.selectionFacet;
    expect(s.selectedJournalId, isNull);
    expect(s.showJournals, isTrue);
    expect(s.selectedDays, isEmpty);
    expect(s.hasActiveFilter, isFalse);
  });
}
