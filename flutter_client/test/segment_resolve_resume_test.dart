// A resolve started before the trip was opened still reaches the map
// (issue #278, Decision 11 of docs/CLIENT_STATE_MAP_PLAN.md).
//
// Polling used to start only from the resolve button, so a resolve started
// earlier in the session, before the app was closed, or on another device left
// its tile spinning until the trip was reopened — and reopening only showed
// whatever had landed by then. load() now seeds the trip's resolve poller from
// the segments the server reports as pending.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'helpers/signed_in.dart';

const _japan = ProjectRef(name: 'Japan');
const _peru = ProjectRef(name: 'Peru');

Map<String, dynamic> get _emptyGeo =>
    {'type': 'FeatureCollection', 'features': <dynamic>[]};

/// Two trips' segments, by trip name, changed by a test as jobs finish.
class _Server extends ProjectService {
  final Map<String, Map<String, Map<String, dynamic>>> trips = {
    'Japan': {
      's1': {
        'id': 's1',
        'segment_type': 'train',
        'route_mode': 'great_circle',
        'route_status': 'pending',
        'start': {'lat': 35.0, 'lon': 139.0},
        'end': {'lat': 34.7, 'lon': 135.5},
      },
    },
    'Peru': {},
  };
  final List<String> metaCalls = [];

  void resolve(String trip, String id) {
    trips[trip]![id] = {
      ...trips[trip]![id]!,
      'route_status': 'resolved',
      'route_mode': 'rail',
      'route_polyline': '[[139.0,35.0],[137.0,35.2],[135.5,34.7]]',
    };
  }

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    metaCalls.add(ref.name);
    return jsonDecode(jsonEncode({
      'name': ref.name,
      'activities': <dynamic>[],
      'items': [
        for (final s in trips[ref.name]!.values)
          {'item_type': 'segment', 'segment': s},
      ],
    })) as Map<String, dynamic>;
  }

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async => _emptyGeo;

  @override
  Future<Map<String, dynamic>> getSimplifiedGeo(ProjectRef ref, double zoom,
          {Object? bbox}) async =>
      _emptyGeo;

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _emptyGeo;
}

ProjectNotifier _notifier(_Server server) => ProjectNotifier(server)
  ..loadRetryBackoff = const []
  ..zoomRefetchDebounce = const Duration(hours: 1);

String? _drawnMode(ProjectNotifier n, String segId) {
  for (final f in (n.geo?['features'] as List? ?? const [])) {
    final props = (f as Map)['properties'] as Map;
    if (props['segment_id'] == segId) return props['route_mode'] as String?;
  }
  return null;
}

String? _itemStatus(ProjectNotifier n, String segId) => n.items
    .map((i) => i['segment'] as Map)
    .firstWhere((s) => s['id'] == segId)['route_status'] as String?;

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    projectDataCache.resetForTest();
    signInAs(1,
        httpClient: MockClient((req) async => http.Response('{}', 200)));
  });

  testWidgets('a trip loaded with a pending segment polls and applies it',
      (tester) async {
    final server = _Server();
    final n = _notifier(server);

    await n.load(_japan);
    await tester.pump(const Duration(seconds: 1)); // the background geo phase
    expect(_itemStatus(n, 's1'), 'pending');
    final loadCalls = server.metaCalls.length;

    await tester.pump(const Duration(minutes: 4));
    expect(server.metaCalls.length, greaterThan(loadCalls),
        reason: 'the load started polling for the pending segment');
    expect(_itemStatus(n, 's1'), 'pending');

    server.resolve('Japan', 's1');
    await tester.pump(const Duration(seconds: 15));

    expect(_itemStatus(n, 's1'), 'resolved');
    expect(_drawnMode(n, 's1'), 'rail');
    expect(n.error, isNull);

    final calls = server.metaCalls.length;
    await tester.pump(const Duration(minutes: 5));
    expect(server.metaCalls.length, calls, reason: 'nothing left to poll for');
    n.dispose();
  });

  testWidgets('opening another trip stops it', (tester) async {
    final server = _Server();
    final n = _notifier(server);

    await n.load(_japan);
    await tester.pump(const Duration(seconds: 3));
    await n.load(_peru);
    await tester.pump(const Duration(seconds: 1));
    server.metaCalls.clear();

    await tester.pump(const Duration(minutes: 5));
    expect(server.metaCalls, isEmpty);
    n.dispose();
  });

  testWidgets('reloading the same trip keeps one poller', (tester) async {
    final server = _Server();
    final n = _notifier(server);

    await n.load(_japan);
    await n.load(_japan);
    await tester.pump(const Duration(seconds: 1));
    server.metaCalls.clear();

    await tester.pump(const Duration(seconds: 30));
    expect(server.metaCalls, hasLength(10), reason: 'one poll every 3 s');
    n.dispose();
  });
}
