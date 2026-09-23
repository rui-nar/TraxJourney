// Regression tests for the silent half of the concurrent-delete bug.
//
// Deleting several segments at once made every request but one 409 (the server
// took a project-wide optimistic lock per delete — fixed on that side too).
// deleteSegment only recorded the failure in `notifier.error`, which nothing
// renders unless the item list is empty, and the segment had already been
// removed locally by removeSegmentLocally before the undo window opened. So a
// refused delete looked exactly like a successful one until the next full
// reload brought the segment back with no explanation.
//
// The fix mirrors what addSegment/updateSegment already do: roll the optimistic
// removal back — reloading instead would resurrect the *other* segments whose
// undo windows are still open — and leave `error` set for the caller to show.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

Map<String, dynamic> _segmentItem(String id) => {
      'item_type': 'segment',
      'segment': {
        'id': id,
        'segment_type': 'flight',
        'label': 'seg $id',
        'start': {'lat': 1.0, 'lon': 2.0},
        'end': {'lat': 3.0, 'lon': 4.0},
      },
    };

Map<String, dynamic> _activityItem(String id) =>
    {'item_type': 'activity', 'activity_id': id};

Map<String, dynamic> _geoWith(List<String> segIds) => {
      'type': 'FeatureCollection',
      'features': [
        for (final id in segIds)
          {
            'type': 'Feature',
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [2.0, 1.0],
                [4.0, 3.0]
              ]
            },
            'properties': {'type': 'segment', 'segment_id': id},
          },
      ],
    };

/// An api whose DELETE always fails with [status].
ApiClient _failingDelete(int status) => ApiClient(
    baseUrl: '',
    httpClient: MockClient((req) async {
      if (req.method == 'DELETE') {
        return http.Response(jsonEncode({'detail': 'nope'}), status);
      }
      return http.Response(jsonEncode({}), 200);
    }));

ProjectNotifier _notifier() => ProjectNotifier(ProjectService())..ref = _ref;

void main() {
  setUp(() => projectDataCache.resetForTest());

  test('a failed delete puts the segment back at its original index', () async {
    api = _failingDelete(409);
    final notifier = _notifier()
      ..items = [_activityItem('a'), _segmentItem('s1'), _activityItem('b')]
      ..geo = _geoWith(['s1']);

    notifier.removeSegmentLocally('s1');
    expect(notifier.items.map((i) => i['item_type']), ['activity', 'activity']);

    await notifier.deleteSegment('s1');

    expect(notifier.items.map((i) => i['item_type']),
        ['activity', 'segment', 'activity'],
        reason: 'the server still has it, so the timeline must show it again');
    expect(notifier.items[1]['segment']['id'], 's1');
  });

  test('a failed delete restores the map feature too', () async {
    api = _failingDelete(500);
    final notifier = _notifier()
      ..items = [_segmentItem('s1')]
      ..geo = _geoWith(['s1']);

    notifier.removeSegmentLocally('s1');
    expect((notifier.geo!['features'] as List), isEmpty);

    await notifier.deleteSegment('s1');

    final ids = (notifier.geo!['features'] as List)
        .map((f) => (f as Map)['properties']['segment_id'])
        .toList();
    expect(ids, ['s1'],
        reason: 'a tombstoned segment stays hidden through every geo rebuild');
  });

  test('a failed delete leaves an error for the caller to surface', () async {
    api = _failingDelete(409);
    final notifier = _notifier()
      ..items = [_segmentItem('s1')]
      ..geo = _geoWith(['s1']);

    var notified = false;
    notifier.addListener(() => notified = true);
    notifier.removeSegmentLocally('s1');

    await notifier.deleteSegment('s1');

    expect(notifier.error, isNotNull);
    expect(notified, isTrue);
  });

  test('a 404 means it is already gone — the removal stands', () async {
    api = _failingDelete(404);
    final notifier = _notifier()
      ..items = [_segmentItem('s1')]
      ..geo = _geoWith(['s1']);

    notifier.removeSegmentLocally('s1');

    await notifier.deleteSegment('s1');

    expect(notifier.items, isEmpty);
    expect(notifier.error, isNull);
  });

  test('a successful delete keeps the segment gone and sets no error', () async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((_) async => http.Response('', 204)));
    final notifier = _notifier()
      ..items = [_segmentItem('s1'), _segmentItem('s2')]
      ..geo = _geoWith(['s1', 's2']);

    notifier.removeSegmentLocally('s1');
    await notifier.deleteSegment('s1');

    expect(notifier.items.map((i) => i['segment']['id']), ['s2']);
    expect(notifier.error, isNull);
  });

  test('a reload that already restored the segment does not duplicate it',
      () async {
    api = _failingDelete(409);
    final notifier = _notifier()
      ..items = [_segmentItem('s1')]
      ..geo = _geoWith(['s1']);

    notifier.removeSegmentLocally('s1');
    // Something else (an Undo, a poll) reloaded the list from the server while
    // the DELETE was in flight.
    notifier.items = [_segmentItem('s1')];

    await notifier.deleteSegment('s1');

    expect(notifier.items, hasLength(1));
  });
}
