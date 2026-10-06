// The items facet holds the trip's content (issue #294, Decisions 17, 19 and
// 23 of docs/CLIENT_STATE_MAP_PLAN.md): load() fills it, clear() empties it,
// and each kind of change bumps only its own part's version — the item list,
// the activity data, people and groups, or the day-meta — and only this
// facet, or this facet and the geometry where the geometry changes too.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/facets/project_facet.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import '../../helpers/signed_in.dart';

const _me = 3;
const _ref = ProjectRef(name: 'Trip');

// Typed as decoded JSON is: the load decrypts memory text in place.
Map<String, dynamic> _payload() => {
      'name': 'Trip',
      'lock_version': 1,
      'trip_start': '2024-06-01',
      'trip_end': '2024-06-02',
      'activities': [
        {
          'id': '1',
          'type': 'Ride',
          'start_date_local': '2024-06-01T08:00:00',
          'distance': 12000,
          'total_elevation_gain': 300,
          // A profile on /meta: no elevation fetch runs after the load.
          'elevation_profile': [
            [0, 100],
            [12, 140],
          ],
        },
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': '1'},
        {
          'item_type': 'memory',
          'memory': <String, dynamic>{'id': 'm1', 'date': '2024-06-01', 'name': 'Lunch'},
        },
        {
          'item_type': 'segment',
          'segment': <String, dynamic>{'id': 's1', 'date': '2024-06-02', 'segment_type': 'train'},
        },
        {
          'item_type': 'encounter',
          'encounter': {'id': 'e1', 'date': '2024-06-02', 'person_id': 1},
        },
      ],
      'people': [
        {'id': 1, 'name': 'Alice'},
        {'id': 2, 'name': 'Bob'},
      ],
      'groups': [
        {'id': 10, 'name': 'Crew'},
      ],
      'day_meta': {
        '2024-06-01': {
          'tags': ['alps'],
          'sleeping': 'Hotel',
        },
        '2024-06-02': <String, dynamic>{},
      },
      'sleeping_options': ['Hotel', 'Tent'],
      'sleeping_option_groups': {'Hotel': 'Indoors', 'Tent': 'Outdoors'},
      'counters': [
        {'name': 'Passes', 'start': 0},
      ],
    };

class _Service extends ProjectService {
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

/// Exposes the elevation merge the view and shared subclasses call.
class _Notifier extends ProjectNotifier {
  _Notifier() : super(_Service());

  Future<void> mergeElevation(List<Map<String, dynamic>> full) =>
      applyFullActivities(full,
          ref: currentLoadKey!, token: currentLoadToken);
}

Future<_Notifier> _loaded() async {
  final n = _Notifier()..loadRetryBackoff = const [Duration(milliseconds: 1)];
  await n.load(_ref);
  await pumpEventQueue();
  return n;
}

/// Every facet's version, and the items facet's four.
typedef _Versions = ({
  int geo,
  int selection,
  int style,
  int elevation,
  int items,
  int list,
  int activities,
  int people,
  int dayMeta,
});

_Versions _versions(ProjectNotifier n) => (
      geo: n.geoFacet.version,
      selection: n.selectionFacet.version,
      style: n.styleFacet.version,
      elevation: n.elevationFacet.version,
      items: n.itemsFacet.version,
      list: n.itemsFacet.listVersion,
      activities: n.itemsFacet.activitiesVersion,
      people: n.itemsFacet.peopleVersion,
      dayMeta: n.itemsFacet.dayMetaVersion,
    );

/// Which of [_Versions] moved from [a] to [b].
Set<String> _moved(_Versions a, _Versions b) => {
      if (a.geo != b.geo) 'geo',
      if (a.selection != b.selection) 'selection',
      if (a.style != b.style) 'style',
      if (a.elevation != b.elevation) 'elevation',
      if (a.items != b.items) 'items',
      if (a.list != b.list) 'list',
      if (a.activities != b.activities) 'activities',
      if (a.people != b.people) 'people',
      if (a.dayMeta != b.dayMeta) 'dayMeta',
    };

Map<String, dynamic> _segmentFeature(String id) => {
      'type': 'Feature',
      'properties': {'type': 'segment', 'segment_id': id},
      'geometry': {
        'type': 'LineString',
        'coordinates': [
          [7.0, 45.0],
          [7.1, 45.1],
        ],
      },
    };

void main() {
  setUp(() {
    signInAs(_me);
    SharedPreferences.setMockInitialValues({});
  });

  test('a fresh facet is empty', () {
    final n = ProjectNotifier(_Service());
    addTearDown(n.dispose);
    final f = n.itemsFacet;
    expect(f.activities, isEmpty);
    expect(f.items, isEmpty);
    expect(f.people, isEmpty);
    expect(f.groups, isEmpty);
    expect(f.tripStart, isNull);
    expect(f.tripEnd, isNull);
    expect(f.dayMeta, isEmpty);
    expect(f.sleepingOptions, isEmpty);
    expect(f.sleepingOptionGroups, isEmpty);
    expect(f.counters, isEmpty);
    expect(f.orderedDayKeys(), isEmpty);
    expect(f.hasFilterableContent, isFalse);
  });

  test('load() fills the content and what is derived from it', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final f = n.itemsFacet;
    expect(f.activities.single['id'], '1');
    expect(f.items, hasLength(4));
    expect(f.people.map((p) => p['name']), ['Alice', 'Bob']);
    expect(f.groups.single['name'], 'Crew');
    expect((f.tripStart, f.tripEnd), ('2024-06-01', '2024-06-02'));
    expect(f.dayMeta.keys, ['2024-06-01', '2024-06-02']);
    expect(f.sleepingOptions, ['Hotel', 'Tent']);
    expect(f.sleepingOptionGroups, {'Hotel': 'Indoors', 'Tent': 'Outdoors'});
    expect(f.counters.single['name'], 'Passes');

    expect(f.availableTags, ['alps']);
    expect(f.availableSleepingModes, ['Hotel', 'No data']);
    expect(f.availableActivityTypes, ['ride']);
    expect(f.availableSources, ['strava']);
    expect(f.availableTransportationMeans, ['train']);
    expect(f.hasFilterableContent, isTrue);
    expect(f.effectiveTagsFor('2024-06-02'), ['alps']);
    expect(f.dayHasOwnTags('2024-06-01'), isTrue);
    expect(f.dayHasOwnTags('2024-06-02'), isFalse);
    expect(f.dayStats('2024-06-01'), (distanceKm: 12.0, elevationM: 300.0));
    expect(f.orderedDayKeys(), ['2024-06-01', '2024-06-02']);
  });

  test('an item-list change bumps only the list version', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = _versions(n);

    n.removeItemLocally(1); // the memory

    expect(_moved(before, _versions(n)), {'items', 'list'});
    expect(n.itemsFacet.items, hasLength(3));
  });

  test('an item change that moves the geometry bumps the list and the geo',
      () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    n.geoFacetWriter.replace({
      'type': 'FeatureCollection',
      'features': [_segmentFeature('s1')],
    }, GeoLod.lowRes);
    final before = _versions(n);

    n.removeSegmentLocally('s1');

    expect(_moved(before, _versions(n)), {'items', 'list', 'geo'});
    expect(n.itemsFacet.items.where((i) => i['item_type'] == 'segment'),
        isEmpty);
  });

  test('an elevation merge bumps only the activities version', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = _versions(n);
    final list = n.itemsFacet.items;

    await n.mergeElevation([
      {
        ..._payload()['activities'][0] as Map<String, dynamic>,
        'elevation_profile': [
          [0, 100],
          [6, 180],
          [12, 140],
        ],
      },
    ]);

    expect(_moved(before, _versions(n)), {'items', 'activities', 'elevation'});
    expect(n.itemsFacet.items, same(list));
    expect(n.itemsFacet.activities.single['elevation_profile'], hasLength(3));
  });

  test('a people change bumps only the people version', () async {
    signInAs(_me,
        httpClient: MockClient((req) async => http.Response('{}', 200)));
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = _versions(n);
    final list = n.itemsFacet.items;

    // Bob has no encounters, so the item list stays as it is. Checked before
    // the delete's reload, which adopts the whole trip again.
    final deleting = n.deletePerson(2);
    expect(_moved(before, _versions(n)), {'items', 'people'});
    expect(n.itemsFacet.items, same(list));
    expect(n.itemsFacet.people.single['name'], 'Alice');
    await deleting;
  });

  test('a person with encounters takes them along: people and list',
      () async {
    signInAs(_me,
        httpClient: MockClient((req) async => http.Response('{}', 200)));
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = _versions(n);

    final deleting = n.deletePerson(1);
    expect(_moved(before, _versions(n)), {'items', 'people', 'list'});
    expect(n.itemsFacet.items.where((i) => i['item_type'] == 'encounter'),
        isEmpty);
    await deleting;
  });

  test('a day-note save bumps only the day-meta version', () async {
    signInAs(_me, httpClient: MockClient((req) async {
      expect((req.method, req.url.path), ('PATCH', '/api/projects/Trip/day-meta'));
      return http.Response(
          jsonEncode({
            'day_meta': {
              '2024-06-01': {
                'tags': ['alps'],
                'sleeping': 'Hotel',
              },
              '2024-06-02': {'note': 'Rain all day'},
            },
          }),
          200);
    }));
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = _versions(n);

    await n.saveDayMeta(days: {
      '2024-06-02': {'note': 'Rain all day'},
    });

    expect(_moved(before, _versions(n)), {'items', 'dayMeta'});
    expect(n.itemsFacet.dayMeta['2024-06-02'], {'note': 'Rain all day'});
  });

  test('a trip-dates change bumps only the day-meta version', () async {
    signInAs(_me,
        httpClient: MockClient((req) async => http.Response('{}', 200)));
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = _versions(n);

    await n.setTripDates('2024-05-31', '2024-06-02');

    expect(_moved(before, _versions(n)), {'items', 'dayMeta'});
    expect(n.itemsFacet.tripStart, '2024-05-31');
  });

  test('a change notifies the facet once, not the root', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    var root = 0, facet = 0;
    n.addListener(() => root++);
    n.itemsFacet.addListener(() => facet++);

    // Two writes — the list and the activities — and one notify.
    n.removeItemLocally(0); // the activity
    expect((root, facet), (0, 1));
    expect(n.itemsFacet.activities, isEmpty);
  });

  test('clear() empties the content and moves every version on', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = _versions(n);

    n.clear();

    final f = n.itemsFacet;
    expect(f.activities, isEmpty);
    expect(f.items, isEmpty);
    expect(f.people, isEmpty);
    expect(f.groups, isEmpty);
    expect((f.tripStart, f.tripEnd), (null, null));
    expect(f.dayMeta, isEmpty);
    expect(f.sleepingOptions, isEmpty);
    expect(f.sleepingOptionGroups, isEmpty);
    expect(f.counters, isEmpty);
    expect(f.orderedDayKeys(), isEmpty);
    expect(f.dayStats('2024-06-01'), (distanceKm: 0.0, elevationM: 0.0));
    final after = _versions(n);
    expect(after.list, greaterThan(before.list));
    expect(after.activities, greaterThan(before.activities));
    expect(after.people, greaterThan(before.people));
    expect(after.dayMeta, greaterThan(before.dayMeta));
  });

  test('a write after dispose() moves no version', () {
    final n = ProjectNotifier(_Service());
    final writer = n.itemsFacetWriter;
    final f = n.itemsFacet;
    n.dispose();

    writer.setItems([
      {'item_type': 'memory'},
    ]);

    expect((f.version, f.listVersion), (0, 0));
  });
}
