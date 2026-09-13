/// A saved source filter that no longer matches anything (#260, unit 6 review).
///
/// The sheet stops offering the Source section once a trip is down to one
/// source, so a restored 'gpx' on a trip whose only import has been deleted
/// would filter every day out of the list with no chip left to untick. Dropping
/// it in memory is only half the job: left in storage it comes back to life the
/// next time the trip gains an activity from that source, narrowing the list for
/// a filter the user never re-ticked.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import '../helpers/signed_in.dart';

const _ref = ProjectRef(name: 'Trip');
// Account 3's own trip: UI state is keyed by account, owner and name (#409).
const _key = 'project_ui_state_3:3:Trip';

Map<String, dynamic> _emptyGeo() =>
    {'type': 'FeatureCollection', 'features': <dynamic>[]};

/// Serves one trip whose activities the test controls, so a source can be
/// removed from it between loads the way deleting an import does.
class _Service extends ProjectService {
  _Service(this.activities);

  List<Map<String, dynamic>> activities;

  Map<String, dynamic> _payload() => {
        'name': 'Trip',
        'activities': activities,
        'items': [
          for (final a in activities)
            {'item_type': 'activity', 'activity_id': a['id']},
        ],
        'day_meta': {
          for (final a in activities)
            (a['start_date_local'] as String).substring(0, 10):
                <String, dynamic>{},
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
      _emptyGeo();

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _emptyGeo();
}

Map<String, dynamic> _activity({required int id, String? source}) => {
      'id': id,
      'name': 'An activity',
      'type': 'Ride',
      'start_date_local': '2026-06-0${id.abs()}T09:00:00',
      if (source != null) 'source': source,
    };

void main() {
  setUp(() => signInAs(3));

  test('the stale source is dropped AND taken out of storage', () async {
    // The fixture carries a selection too. load() nulls the selection fields
    // before fetching and _saveUiState builds its payload synchronously, so a
    // write-back that fires before the restores below have read them back
    // persists those nulls and destroys the saved selection — the pruning load
    // would take the day and activity down with the stale filter.
    SharedPreferences.setMockInitialValues({
      _key: '{"sources":["gpx"],"tags":[],'
          '"selectedDay":"2026-06-01","selectedActivityId":"1"}'
    });
    final service = _Service([_activity(id: 1)]); // Strava only: the import is gone
    final notifier = ProjectNotifier(service);

    await notifier.load(_ref);
    await pumpEventQueue();

    expect(notifier.sourceFilter, isEmpty);
    expect(notifier.hasActiveFilter, isFalse);
    expect(notifier.selectedDay, '2026-06-01');

    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString(_key);
    expect(saved, isNot(contains('gpx')),
        reason: 'a filter dropped in memory only comes back on the next import');
    expect(saved, contains('"selectedDay":"2026-06-01"'),
        reason: 'pruning a filter must not cost the user their selection');
    expect(saved, contains('"selectedActivityId":"1"'));
  });

  test('and the selection is still there on the load after that', () async {
    SharedPreferences.setMockInitialValues({
      _key: '{"sources":["gpx"],"tags":[],'
          '"selectedDay":"2026-06-01","selectedActivityId":"1"}'
    });
    final service = _Service([_activity(id: 1)]);
    final notifier = ProjectNotifier(service);

    await notifier.load(_ref);
    await pumpEventQueue();
    // Switch away and back, with nothing touched in between.
    await ProjectNotifier(service).load(_ref);
    await pumpEventQueue();
    final second = ProjectNotifier(service);
    await second.load(_ref);
    await pumpEventQueue();

    expect(second.selectedDay, '2026-06-01');
    expect(second.selectedActivityId, '1');
  });

  test('and so does not re-apply itself when a new import arrives', () async {
    SharedPreferences.setMockInitialValues(
        {_key: '{"sources":["gpx"],"tags":[]}'});
    final service = _Service([_activity(id: 1)]);
    final notifier = ProjectNotifier(service);

    await notifier.load(_ref);
    await pumpEventQueue();

    // The user imports a GPX file again, and the trip is reloaded.
    service.activities = [_activity(id: 1), _activity(id: 2, source: 'gpx')];
    await notifier.load(_ref);
    await pumpEventQueue();

    expect(notifier.sourceFilter, isEmpty,
        reason: 'the user never re-ticked it');
    expect(notifier.selectedDays, isEmpty);
  });

  test('a filter the trip still matches survives the round trip', () async {
    SharedPreferences.setMockInitialValues(
        {_key: '{"sources":["gpx"],"tags":[]}'});
    final service =
        _Service([_activity(id: 1), _activity(id: 2, source: 'gpx')]);
    final notifier = ProjectNotifier(service);

    await notifier.load(_ref);
    await pumpEventQueue();

    expect(notifier.sourceFilter, {'gpx'});
    expect(notifier.selectedDays, {'2026-06-02'});
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString(_key), contains('gpx'));
  });
}
