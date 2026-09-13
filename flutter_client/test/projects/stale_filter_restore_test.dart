/// A saved tag, sleeping, activity-type or transport filter that no longer
/// matches anything (#409).
///
/// Filter a trip to hikes, delete the last hike, reload: the saved 'hike'
/// restored verbatim, matched no day and emptied the list, and the filter sheet
/// — which offers what the trip holds — had no 'hike' chip to untick. Only
/// "Clear all" got out, taking every other dimension with it. The source
/// dimension was fixed for this in #406; these pin the same contract for the
/// rest, through the real load() so the write-back and its ordering are covered
/// and not just the pruning seam.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_platform_interface.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_filters.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/shared/shared_project_screen.dart';

import '../helpers/signed_in.dart';

/// The signed-in account for every test unless one says otherwise.
const _me = 3;
const _ref = ProjectRef(name: 'Trip');
// UI state is keyed by account, owner and trip name (#409 review).
const _key = 'project_ui_state_$_me:$_me:Trip';
const _day1 = '2026-06-01';
const _day2 = '2026-06-02';

Map<String, dynamic> _emptyGeo() =>
    {'type': 'FeatureCollection', 'features': <dynamic>[]};

/// What the fake server holds, mutable between loads the way editing the trip
/// changes it.
class _Trip {
  final activities = <Map<String, dynamic>>[
    {
      'id': 1,
      'name': 'Morning ride',
      'type': 'Ride',
      'start_date_local': '${_day1}T09:00:00',
    },
  ];
  final segments = <Map<String, dynamic>>[
    {'id': 11, 'segment_type': 'train', 'date': _day1},
  ];
  final dayMeta = <String, Map<String, dynamic>>{
    _day1: {
      'tags': ['beach'],
      'sleeping': 'Hotel',
    },
    // No tags of its own, so it inherits 'beach'; no sleeping mode, so it
    // filters as 'No data'.
    _day2: {},
  };
}

class _Service extends ProjectService {
  _Service(this.trip, {this.name = 'Trip', this.callerRole});

  final _Trip trip;
  String name;

  /// What the server says the caller is on this trip. It always sends one;
  /// null here leaves the ref's own role standing.
  final String? callerRole;

  /// Every fetch fails, the way it does with no network.
  bool offline = false;

  /// While set, /meta does not answer until it completes — a slow network.
  Completer<void>? metaGate;

  Map<String, dynamic> _payload() => {
        'name': name,
        'lock_version': 1,
        if (callerRole != null) 'caller_role': callerRole,
        'activities': [for (final a in trip.activities) {...a}],
        'items': [
          for (final a in trip.activities)
            {'item_type': 'activity', 'activity_id': a['id']},
          for (final s in trip.segments)
            {'item_type': 'segment', 'segment': {...s}},
        ],
        'day_meta': {
          for (final e in trip.dayMeta.entries) e.key: {...e.value},
        },
        'people': <dynamic>[],
        'groups': <dynamic>[],
      };

  Future<T> _answer<T>(T Function() value) async {
    if (offline) throw Exception('offline');
    return value();
  }

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    await metaGate?.future;
    return _answer(_payload);
  }

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) =>
      _answer(_payload);

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) =>
      _answer(_emptyGeo);

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) =>
      _answer(_emptyGeo);
}

/// One filter dimension: a value the trip holds, one it does not, and how the
/// trip comes to hold the stale one again.
class _Dimension {
  const _Dimension({
    required this.name,
    required this.key,
    required this.held,
    required this.stale,
    required this.read,
    required this.regain,
  });

  final String name;
  final String key; // as _saveUiState writes it
  final String held;
  final String stale;
  final Set<String> Function(ProjectNotifier) read;
  final void Function(_Trip) regain;
}

final _dimensions = [
  _Dimension(
    name: 'activity type',
    key: 'activityTypes',
    held: 'ride',
    stale: 'hike',
    read: (n) => n.activityTypeFilter,
    // Capitalised as the server sends it; the filter matches lower-cased.
    regain: (t) => t.activities.add({
      'id': 2,
      'name': 'Afternoon hike',
      'type': 'Hike',
      'start_date_local': '${_day2}T14:00:00',
    }),
  ),
  _Dimension(
    name: 'transport',
    key: 'transport',
    held: 'train',
    stale: 'flight',
    read: (n) => n.transportFilter,
    regain: (t) =>
        t.segments.add({'id': 12, 'segment_type': 'flight', 'date': _day2}),
  ),
  _Dimension(
    name: 'tag',
    key: 'tags',
    held: 'beach',
    stale: 'museum',
    read: (n) => n.tagFilter,
    regain: (t) => t.dayMeta[_day2] = {
      'tags': ['museum'],
    },
  ),
  _Dimension(
    name: 'sleeping mode',
    key: 'sleeping',
    held: 'Hotel',
    stale: 'Camping',
    read: (n) => n.sleepingFilter,
    regain: (t) => t.dayMeta[_day2] = {'sleeping': 'Camping'},
  ),
];

Future<Map<String, dynamic>> _stored() async {
  final prefs = await SharedPreferences.getInstance();
  return jsonDecode(prefs.getString(_key)!) as Map<String, dynamic>;
}

Future<ProjectNotifier> _loaded(_Service service,
    {ProjectRef ref = _ref}) async {
  final notifier = ProjectNotifier(service)
    ..loadRetryBackoff = const [Duration(milliseconds: 1)];
  await notifier.load(ref);
  await pumpEventQueue();
  return notifier;
}

void main() {
  setUp(() => signInAs(_me));

  for (final d in _dimensions) {
    group('a saved ${d.name} filter', () {
      test('drops the value the trip no longer holds, in memory and on disk',
          () async {
        // The fixture carries a selection too. load() nulls the selection
        // fields before fetching and _saveUiState builds its payload
        // synchronously, so a write-back fired before the selection restores
        // would persist those nulls over the user's day and activity.
        SharedPreferences.setMockInitialValues({
          _key: jsonEncode({
            d.key: [d.stale, d.held],
            'selectedDay': _day1,
            'selectedActivityId': '1',
          }),
        });
        final service = _Service(_Trip());

        final notifier = await _loaded(service);

        expect(d.read(notifier), {d.held});
        expect(notifier.selectedDays, isNotEmpty,
            reason: 'the value the trip still holds keeps narrowing');
        expect(notifier.selectedDay, _day1);
        expect(notifier.selectedActivityId, '1');

        final stored = await _stored();
        expect(stored[d.key], [d.held],
            reason: 'a value dropped in memory only comes back with the data');
        expect(stored['selectedDay'], _day1,
            reason: 'pruning a filter must not cost the user their selection');
        expect(stored['selectedActivityId'], '1');

        // And the next open of the trip still has the selection.
        final next = await _loaded(service);
        expect(next.selectedDay, _day1);
        expect(next.selectedActivityId, '1');
        expect(d.read(next), {d.held});
      });

      test('does not re-apply itself when the trip regains matching data',
          () async {
        SharedPreferences.setMockInitialValues({
          _key: jsonEncode({
            d.key: [d.stale],
          }),
        });
        final service = _Service(_Trip());
        final notifier = await _loaded(service);
        expect(notifier.hasActiveFilter, isFalse);

        d.regain(service.trip);
        await notifier.load(_ref);
        await pumpEventQueue();

        expect(d.read(notifier), isEmpty,
            reason: 'the user never re-ticked it');
        expect(notifier.hasActiveFilter, isFalse);
      });

      test('keeps a value the trip still holds, and leaves storage alone',
          () async {
        final saved = jsonEncode({
          d.key: [d.held],
          'selectedDay': _day1,
        });
        SharedPreferences.setMockInitialValues({_key: saved});

        final notifier = await _loaded(_Service(_Trip()));

        expect(d.read(notifier), {d.held});
        expect(notifier.selectedDays, isNotEmpty);
        final prefs = await SharedPreferences.getInstance();
        expect(prefs.getString(_key), saved);
      });
    });
  }

  test('every dimension is pruned in the same load, and one write keeps it',
      () async {
    SharedPreferences.setMockInitialValues({
      _key: jsonEncode({
        for (final d in _dimensions) d.key: [d.stale, d.held],
        'sources': ['gpx', 'strava'],
        'selectedDay': _day1,
      }),
    });

    final notifier = await _loaded(_Service(_Trip()));

    for (final d in _dimensions) {
      expect(d.read(notifier), {d.held}, reason: d.name);
    }
    expect(notifier.sourceFilter, {'strava'});
    expect(notifier.selectedDays, {_day1});
    final stored = await _stored();
    for (final d in _dimensions) {
      expect(stored[d.key], [d.held], reason: d.name);
    }
    expect(stored['sources'], ['strava']);
    expect(stored['selectedDay'], _day1);
  });

  group('tags prune against availableTags, and that is safe', () {
    // Tags match on *effective* tags (issue #18): a day with none of its own
    // inherits the nearest earlier day's, and an explicit empty set stops that
    // (#203). availableTags is only the tags days own, so pruning against it
    // would be wrong if any day could match a tag nobody owns. None can: an
    // inherited tag is some earlier day's own tag, and a day's own tags are its
    // effective tags. These pin that, so a change to either side is noticed.
    ProjectNotifier notifierWith(Map<String, Map<String, dynamic>> dayMeta) =>
        ProjectNotifier(ProjectService())..dayMeta = dayMeta;

    final dayMeta = <String, Map<String, dynamic>>{
      '2026-06-01': {
        'tags': ['beach', 'city'],
      },
      '2026-06-02': {}, // inherits beach, city
      '2026-06-03': {'tags': <String>[]}, // cleared: inherits nothing
      '2026-06-04': {}, // skips the cleared day, inherits beach, city
      '2026-06-05': {
        'tags': ['museum'],
      },
      '2026-06-06': {}, // inherits museum
    };

    test('the effective tags across the trip are exactly availableTags', () {
      final notifier = notifierWith(dayMeta);

      final effective =
          dayMeta.keys.expand(notifier.effectiveTagsFor).toSet();

      expect(effective, notifier.availableTags.toSet());
    });

    test('restore keeps a tag if and only if it matches a day', () {
      for (final tag in ['beach', 'city', 'museum', 'hiking']) {
        final probe = notifierWith(dayMeta)..setFilters(tags: {tag});
        final matches = probe.selectedDays.isNotEmpty;

        final notifier = notifierWith(dayMeta);
        final pruned = notifier.restoreFilters(ProjectFilters(tags: {tag}));

        expect(notifier.tagFilter.contains(tag), matches, reason: tag);
        expect(pruned, !matches, reason: tag);
        expect(notifier.selectedDays, probe.selectedDays, reason: tag);
      }
    });
  });
  group('whose saved state a load reads and writes', () {
    // UI state was keyed on the trip name alone, and neither names nor devices
    // are unique to one person: a companion's trip is likely to share a name
    // with yours (#106), a share link's ref is only the trip's name, and one
    // browser can hold two accounts (#93). Each of those read your state,
    // pruned it against someone else's data, and wrote the loss back. The
    // state is set up through the app and checked by reopening your own trip,
    // so these hold whatever the key looks like.
    const mine = ProjectRef(name: 'Japan');
    // As AppScreen passes it: ?owner=7 from the URL, and a role guessed before
    // /meta answers — "editor" for anyone else's trip.
    const theirs = ProjectRef(name: 'Japan', ownerId: 7, role: 'editor');

    Map<String, dynamic> hike(int id) => {
          'id': id,
          'name': 'Temple hike',
          'type': 'Hike',
          'start_date_local': '${_day1}T14:00:00',
        };
    _Service myJapan() => _Service(_Trip()..activities.add(hike(2)),
        name: 'Japan', callerRole: 'owner');
    // Theirs has no hikes, so applying your state to it would prune 'hike'.
    _Service theirJapan({String callerRole = 'editor'}) =>
        _Service(_Trip(), name: 'Japan', callerRole: callerRole);

    /// Filters your own "Japan" to hikes and selects the hike, through the app.
    Future<void> saveMyJapan({ProjectRef ref = mine}) async {
      final own = await _loaded(myJapan(), ref: ref);
      own.setFilters(activityTypes: {'hike'});
      own.selectActivity(2);
      await pumpEventQueue();
    }

    /// Reopens your own "Japan" and asserts the state is still there.
    Future<void> expectMyJapanIntact() async {
      final own = await _loaded(myJapan(), ref: mine);
      expect(own.activityTypeFilter, {'hike'},
          reason: "your own trip's saved filter");
      expect(own.selectedActivityId, '2',
          reason: "your own trip's saved selection");
    }

    setUp(() => SharedPreferences.setMockInitialValues({}));

    test('opening a same-named trip shared with you leaves yours alone',
        () async {
      await saveMyJapan();

      final shared = await _loaded(theirJapan(), ref: theirs);
      expect(shared.hasActiveFilter, isFalse,
          reason: 'your filter is not theirs to apply');

      await expectMyJapanIntact();
    });

    test('and theirs keeps state of its own', () async {
      final service = theirJapan();
      final shared = await _loaded(service, ref: theirs);
      shared.setFilters(activityTypes: {'ride'});
      shared.selectActivity(1);
      await pumpEventQueue();

      final again = await _loaded(service, ref: theirs);
      expect(again.activityTypeFilter, {'ride'});
      expect(again.selectedActivityId, '1');
    });

    for (final role in ['viewer', 'co-owner']) {
      test('a $role gets their state back too, though /meta corrects the role',
          () async {
        // The role guessed before /meta is "editor"; the server says otherwise.
        // Restore used to check the load was current against the corrected
        // ref, so it never ran for a viewer or co-owner, on any open.
        final service = theirJapan(callerRole: role);
        final first = await _loaded(service, ref: theirs);
        first.setFilters(activityTypes: {'ride'});
        first.selectActivity(1);
        await pumpEventQueue();

        final again = await _loaded(service, ref: theirs);

        expect(again.ref?.role, role);
        expect(again.activityTypeFilter, {'ride'});
        expect(again.selectedActivityId, '1');
      });
    }

    test('your own trip opened from the projects list is still your own trip',
        () async {
      // The list gives your own entries your id as owner_id, so from there the
      // trip opens as ?owner=<you>, and until /meta answers the role is a guess
      // — "editor" whenever the profile has no id, as a restored session's
      // does not. A deep link opens the same trip with no owner at all.
      await saveMyJapan();

      final fromList = await _loaded(myJapan(),
          ref: const ProjectRef(name: 'Japan', ownerId: _me, role: 'editor'));

      expect(fromList.activityTypeFilter, {'hike'});
      expect(fromList.selectedActivityId, '2');
    });

    test('a reload that has not heard back from /meta saves nothing over yours',
        () async {
      // The Strava and Polarsteps imports reload with the ref from the URL,
      // whose role defaults to "owner", and pop straight back to the map. A
      // tap on a track while /meta is still out selected an activity — and
      // saved it as your own trip's state.
      await saveMyJapan();
      final service = theirJapan();
      final shared = await _loaded(service, ref: theirs);

      service.metaGate = Completer<void>();
      final reload = shared.load(const ProjectRef(name: 'Japan', ownerId: 7));
      shared.selectActivity(1);
      await pumpEventQueue();
      service.metaGate!.complete();
      await reload;
      await pumpEventQueue();

      await expectMyJapanIntact();
    });

    group('a share link to a same-named trip', () {
      // A share link's notifier loads ProjectRef(name: <token>), and once the
      // public /meta lands its ref is the trip's name with no owner and no
      // caller_role — to the key, your own trip.
      http.Client shareServer() {
        final meta = jsonEncode(theirJapan()._payload()..remove('caller_role'));
        final geo = jsonEncode(_emptyGeo());
        return MockClient((req) async {
          final path = req.url.path;
          if (path == '/api/share/tok/meta' || path == '/api/share/tok') {
            return http.Response(meta, 200);
          }
          if (path.startsWith('/api/share/tok/geo')) {
            return http.Response(geo, 200);
          }
          return http.Response('{}', 404);
        });
      }

      test('neither applies nor rewrites your state when opened', () async {
        signInAs(_me, httpClient: shareServer());
        await saveMyJapan();

        final link = SharedProjectNotifier('tok');
        await link.loadShared();
        await pumpEventQueue();

        expect(link.ref?.name, 'Japan');
        expect(link.hasActiveFilter, isFalse);
        expect(link.selectedActivityId, isNull);
        await expectMyJapanIntact();
      });

      test('and a tap in it is not saved as your trip', () async {
        signInAs(_me, httpClient: shareServer());
        await saveMyJapan();

        final link = SharedProjectNotifier('tok');
        await link.loadShared();
        link.selectActivity(1);
        await pumpEventQueue();

        await expectMyJapanIntact();
      });
    });

    test('another account on this browser neither inherits nor erases yours',
        () async {
      // One notifier throughout, as the app has: it is created once and a
      // logout does not clear it.
      final service = myJapan();
      final notifier = await _loaded(service, ref: mine);
      notifier.setFilters(activityTypes: {'hike'});
      notifier.selectActivity(2);
      await pumpEventQueue();

      // Someone else signs in, in the same tab, and opens their own "Japan" —
      // which has no hikes, so applying your state to it would prune 'hike'.
      signInAs(4);
      service.trip.activities.removeWhere((a) => a['id'] == 2);
      await notifier.load(mine);
      await pumpEventQueue();
      expect(notifier.hasActiveFilter, isFalse, reason: 'not theirs to inherit');
      expect(notifier.selectedActivityId, isNull);
      notifier.selectDay(_day2);
      await pumpEventQueue();

      signInAs(_me);
      service.trip.activities.add(hike(2));
      await notifier.load(mine);
      await pumpEventQueue();
      expect(notifier.activityTypeFilter, {'hike'});
      expect(notifier.selectedActivityId, '2');
    });

    for (final (label, ref, first) in [
      ("a companion's trip from the projects list",
          const ProjectRef(name: 'Japan', ownerId: 7), 7),
      ('an own trip of the same name, deep-linked', mine, _me),
    ]) {
      test('after a logout, the next account to open $label inherits nothing',
          () async {
        // Owner 7 filters their "Japan" to Hotel nights and logs out; a
        // companion logs in in the same tab and opens the same trip, same name
        // and owner. Keeping filters for "the same trip" handed them 7's
        // filter, and the trip still has Hotel nights, so no pruning would
        // ever take it off again once their next tap saved it.
        final service = _Service(_Trip(), name: 'Japan');
        signInAs(first);
        final notifier = await _loaded(service, ref: ref);
        notifier.setFilters(sleeping: {'Hotel'});
        await pumpEventQueue();

        signInAs(4);
        await notifier.load(ref);
        await pumpEventQueue();
        expect(notifier.sleepingFilter, isEmpty);
        expect(notifier.hasActiveFilter, isFalse);

        notifier.selectDay(_day1);
        await pumpEventQueue();
        await notifier.load(ref);
        await pumpEventQueue();
        expect(notifier.sleepingFilter, isEmpty,
            reason: "and nothing of the last account's was saved as theirs");
      });
    }

    group('a reload of the same trip', () {
      // Imports, Undo and Retry reload the trip in place. load() empties the
      // activities but not dayMeta, so the filter badge and the sheet's tag
      // and sleeping chips stay up while /meta is out — seconds, on a slow
      // network (#178).
      test('keeps the filter on while /meta is out, and a tap adds to it',
          () async {
        final service = _Service(_Trip()..activities.add(hike(2)));
        final notifier = await _loaded(service);
        notifier.setFilters(activityTypes: {'hike'});
        await pumpEventQueue();

        service.metaGate = Completer<void>();
        final reload = notifier.load(_ref);
        expect(notifier.activityTypeFilter, {'hike'},
            reason: 'the badge does not blink off for a reload');

        notifier.setFilters(sleeping: {'Hotel'}); // the user taps meanwhile
        service.metaGate!.complete();
        await reload;
        await pumpEventQueue();

        expect(notifier.activityTypeFilter, {'hike'},
            reason: 'resetting at load start, the tap saved over it');
        expect(notifier.sleepingFilter, {'Hotel'});
        final next = await _loaded(service);
        expect(next.activityTypeFilter, {'hike'});
        expect(next.sleepingFilter, {'Hotel'});
      });

      test('keeps the filter through a failed load and its Retry', () async {
        projectDataCache.resetForTest(); // no cached copy: the load fails
        final service = _Service(_Trip()..activities.add(hike(2)));
        final notifier = await _loaded(service);
        notifier.setFilters(activityTypes: {'hike'});
        await pumpEventQueue();

        service.offline = true;
        await notifier.load(_ref);
        await pumpEventQueue();
        expect(notifier.error, isNotNull);
        expect(notifier.activityTypeFilter, {'hike'});

        // Retry, on a network still slow enough to leave the sheet up: a
        // failed load must not have made the retry look like another trip.
        service.offline = false;
        service.metaGate = Completer<void>();
        final retry = notifier.load(_ref);
        expect(notifier.activityTypeFilter, {'hike'},
            reason: 'still on while the retry is out');
        service.metaGate!.complete();
        await retry;
        await pumpEventQueue();
        expect(notifier.error, isNull);
        expect(notifier.activityTypeFilter, {'hike'});
      });
    });

    test('with no account signed in, a trip switch still drops the filter',
        () async {
      // With nothing saved there is no saved state to say a filter is still
      // this trip's, so a load with no key always resets.
      api.clearToken();
      final service = _Service(_Trip()..activities.add(hike(2)));
      final notifier = await _loaded(service);
      notifier.setFilters(activityTypes: {'hike'});

      service.name = 'Other';
      await notifier.load(const ProjectRef(name: 'Other'));
      await pumpEventQueue();

      expect(notifier.hasActiveFilter, isFalse);
    });

    test('with no account signed in, nothing is saved or restored', () async {
      api.clearToken();
      final notifier = await _loaded(_Service(_Trip()));
      notifier.setFilters(activityTypes: {'ride'});
      await pumpEventQueue();

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getKeys(), isEmpty);
    });

    test("switching trips does not carry the last trip's filter", () async {
      // The manage-mode notifier is app-wide. Filter one trip, open another
      // that has nothing saved: the filter stayed on with no day selected,
      // which empties the list — #409's own symptom, on a trip the filter
      // never belonged to.
      final service = _Service(_Trip()..activities.add(hike(2)));
      final notifier = await _loaded(service);
      notifier.setFilters(activityTypes: {'hike'});

      service.name = 'Other';
      await notifier.load(const ProjectRef(name: 'Other'));
      await pumpEventQueue();

      expect(notifier.hasActiveFilter, isFalse);
      expect(notifier.activityTypeFilter, isEmpty);
    });

    group('state saved before the key changed', () {
      const legacyKey = 'project_ui_state_Japan';
      final legacyState = jsonEncode({
        'activityTypes': ['hike', 'kayak'], // no kayak left: pruned on load
        'selectedDay': _day1,
        'selectedActivityId': '2',
      });

      test('restores for your own trip, and moves to the new key', () async {
        SharedPreferences.setMockInitialValues({legacyKey: legacyState});

        final own = await _loaded(myJapan(), ref: mine);
        expect(own.activityTypeFilter, {'hike'});
        expect(own.selectedDay, _day1);
        expect(own.selectedActivityId, '2');

        final prefs = await SharedPreferences.getInstance();
        expect(prefs.getString(legacyKey), isNull,
            reason: 'claimed by the first restore, not read on every open');

        own.selectDay(_day2);
        await pumpEventQueue();
        final next = await _loaded(myJapan(), ref: mine);
        expect(next.selectedDay, _day2);
        expect(next.activityTypeFilter, {'hike'});
      });

      test('is read once even when the restore changes nothing', () async {
        // Nothing stale and no tap: before, only a pruning restore or a user
        // action wrote the new key, so the old one was read again every time.
        SharedPreferences.setMockInitialValues({
          legacyKey: jsonEncode({
            'activityTypes': ['hike'],
            'selectedDay': _day1,
          }),
        });

        await _loaded(myJapan(), ref: mine);
        final prefs = await SharedPreferences.getInstance();
        expect(prefs.getString(legacyKey), isNull);

        // Another account's own "Japan" no longer finds it.
        signInAs(4);
        final theirs = await _loaded(myJapan(), ref: mine);
        expect(theirs.hasActiveFilter, isFalse);

        signInAs(_me);
        final again = await _loaded(myJapan(), ref: mine);
        expect(again.activityTypeFilter, {'hike'});
        expect(again.selectedDay, _day1);
      });

      test('is not read on an offline load', () async {
        // Offline restores do not prune, and the old key is not per account:
        // its tags and sleeping modes could be another account's, applied raw,
        // offered as chips and saved as this account's on the next tap.
        projectDataCache.resetForTest();
        SharedPreferences.setMockInitialValues({
          legacyKey: jsonEncode({
            'tags': ['anniversary-secret'],
            'sleeping': ['Friend'],
          }),
        });
        final service = myJapan();
        projectDataCache.onMetaFetched(mine, service._payload());
        service.offline = true;

        final offline = await _loaded(service, ref: mine);
        expect(offline.offlineFromCache, isTrue);
        expect(offline.hasActiveFilter, isFalse);
        offline.selectDay(_day1);
        await pumpEventQueue();

        final prefs = await SharedPreferences.getInstance();
        expect(prefs.getString(legacyKey), isNotNull,
            reason: 'an offline load neither reads nor deletes it');
        expect(prefs.getString('project_ui_state_$_me:$_me:Japan'),
            isNot(contains('anniversary-secret')));

        // Back online. The tap above gave the trip state of its own, which is
        // what restores; the old key is not read, and is not deleted either.
        service.offline = false;
        await offline.load(mine);
        await pumpEventQueue();
        expect(offline.offlineFromCache, isFalse);
        expect(offline.hasActiveFilter, isFalse,
            reason: "the tap's state wins, and it has no filter");
        expect(offline.selectedDay, _day1, reason: 'the offline tap stands');
        expect(prefs.getString(legacyKey), isNotNull,
            reason: 'nothing deletes a key no restore has claimed');
      });

      test("is left for another account's trip when one already has state",
          () async {
        // Account 4 opens its own "Japan" offline and taps, so its trip has
        // state of its own before it ever reads the old key. Deleting the old
        // key as a leftover then threw away state account 3 had not yet
        // claimed.
        projectDataCache.resetForTest();
        SharedPreferences.setMockInitialValues({legacyKey: legacyState});
        signInAs(4);
        final service = myJapan();
        projectDataCache.onMetaFetched(mine, service._payload());
        service.offline = true;
        final theirs = await _loaded(service, ref: mine);
        theirs.selectDay(_day2);
        await pumpEventQueue();
        service.offline = false;
        await theirs.load(mine);
        await pumpEventQueue();
        expect(theirs.selectedDay, _day2);

        signInAs(_me);
        final own = await _loaded(myJapan(), ref: mine);
        expect(own.activityTypeFilter, {'hike'});
        expect(own.selectedDay, _day1);
        expect(own.selectedActivityId, '2');
      });

      for (final (label, store) in [
        ('throws, as a full localStorage does', _RefusingStore.throwing),
        ('reports failure, as a failed Android commit does',
            _RefusingStore.failing),
      ]) {
        test('survives a migration write the store $label, and a second load',
            () async {
          // The plugin puts a value in its in-memory cache before it calls the
          // store, so reading the new key back cannot confirm the write: the
          // old key was deleted, and after a page load neither was left. The
          // same refused write also sits in the cache for the rest of the
          // session, so a second load (an import, an Undo, a Retry) sees the
          // new key there — and must not take that as leave to delete the old.
          final backing = store({
            'flutter.$legacyKey': jsonEncode({
              'activityTypes': ['hike'],
              'selectedDay': _day1,
            }),
          });
          SharedPreferences.resetStatic();
          SharedPreferencesStorePlatform.instance = backing;

          final own = await _loaded(myJapan(), ref: mine);
          expect(own.activityTypeFilter, {'hike'});

          // What the next page load will find: the store, not the cache.
          var persisted = await backing.getAll();
          expect(persisted.containsKey('flutter.$legacyKey'), isTrue,
              reason: 'nothing was written, so nothing may be deleted');

          await own.load(mine);
          await pumpEventQueue();
          persisted = await backing.getAll();
          expect(persisted.containsKey('flutter.project_ui_state_$_me:$_me:Japan'),
              isFalse,
              reason: 'the store refused every write');
          expect(persisted.containsKey('flutter.$legacyKey'), isTrue,
              reason: 'the only stored copy survives the second load');
        });
      }

      test('restores when your own trip is opened from the projects list',
          () async {
        SharedPreferences.setMockInitialValues({legacyKey: legacyState});

        final own = await _loaded(myJapan(),
            ref: const ProjectRef(name: 'Japan', ownerId: _me));

        expect(own.activityTypeFilter, {'hike'});
      });

      test('is not read for a trip shared with you', () async {
        SharedPreferences.setMockInitialValues({legacyKey: legacyState});

        final shared = await _loaded(
            _Service(_Trip()..activities.add(hike(2)),
                name: 'Japan', callerRole: 'editor'),
            ref: theirs);

        expect(shared.hasActiveFilter, isFalse);
      });
    });
  });

  group('an offline load', () {
    // Offline, the trip is the cached /meta snapshot, which local edits do not
    // refresh. Set a night to Camping, tick the Camping filter, then open the
    // trip with no network: the snapshot predates the Camping night, and
    // pruning against it would delete a filter that is still good.
    setUp(projectDataCache.resetForTest);

    test('keeps a filter the snapshot does not hold, and never saves the loss',
        () async {
      SharedPreferences.setMockInitialValues({
        _key: jsonEncode({
          'sleeping': ['Camping'],
          'selectedDay': _day1,
        }),
      });
      final service = _Service(_Trip()); // the snapshot: no Camping night
      projectDataCache.onMetaFetched(_ref, service._payload());
      service.offline = true;

      final notifier = await _loaded(service);

      expect(notifier.offlineFromCache, isTrue);
      expect(notifier.sleepingFilter, {'Camping'});
      expect((await _stored())['sleeping'], ['Camping']);

      // The next selection change saves whatever is in memory.
      notifier.selectDay(_day2);
      await pumpEventQueue();
      expect((await _stored())['sleeping'], ['Camping'],
          reason: 'a pruned in-memory set would be written here');

      // Back online, where the Camping night is.
      service.offline = false;
      service.trip.dayMeta[_day2] = {'sleeping': 'Camping'};
      await notifier.load(_ref);
      await pumpEventQueue();

      expect(notifier.offlineFromCache, isFalse);
      expect(notifier.sleepingFilter, {'Camping'});
      expect(notifier.selectedDays, {_day2});
    });

    test('and an online load after it still prunes what is really gone',
        () async {
      SharedPreferences.setMockInitialValues({
        _key: jsonEncode({
          'sleeping': ['Camping'],
        }),
      });
      final service = _Service(_Trip());
      projectDataCache.onMetaFetched(_ref, service._payload());
      service.offline = true;
      final notifier = await _loaded(service);
      expect(notifier.sleepingFilter, {'Camping'});

      service.offline = false;
      await notifier.load(_ref);
      await pumpEventQueue();

      expect(notifier.sleepingFilter, isEmpty);
      expect((await _stored())['sleeping'], isEmpty);
    });
  });
}

/// A store that refuses every write, the way a full localStorage or a failed
/// Android commit does.
class _RefusingStore extends InMemorySharedPreferencesStore {
  _RefusingStore.throwing(super.data)
      : _throws = true,
        super.withData();
  _RefusingStore.failing(super.data)
      : _throws = false,
        super.withData();

  final bool _throws;

  @override
  Future<bool> setValue(String valueType, String key, Object value) async {
    if (_throws) throw Exception('QuotaExceededError');
    return false;
  }
}
