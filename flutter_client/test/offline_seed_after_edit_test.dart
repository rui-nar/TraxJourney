// The offline copy is refilled in the background after an edit (I2-R1-1,
// issue #379).
//
// An edit's /meta brings a new lock_version, which clears the trip's
// full-resolution row on disk, and since #379 the refresh after an edit
// fetches simplified geometry and writes none. The only writer of that row is
// the offline seed, which ran only at the end of a load — so a trip edited
// online and opened offline before the next online open had no map. The
// reload after an edit now starts the same disk-only seed.
//
// What an offline open draws is the row asserted here (readCachedGeo reads
// it back through the native store, which `flutter test` has no backend for),
// so the rows reaching the disk seam are the observable.

import 'dart:async';
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

http.Response _json(Object body) => http.Response(jsonEncode(body), 200);

/// One trip whose every write bumps [version]; the full-resolution endpoint
/// waits on [gate] and answers with the version it saw on arrival.
class _Server {
  int version = 1;
  int fullRequests = 0;
  Completer<void> gate = Completer<void>()..complete();

  Map<String, dynamic> _geo(int v) => {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'properties': {'type': 'activity', 'activity_id': '1', 'v': v},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [7.0, 45.0],
                [7.1, 45.1],
              ],
            },
          },
        ],
      };

  ApiClient api() => ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          final path = req.url.path;
          if (path == '/api/projects/Trip/meta') {
            return _json({
              'name': 'Trip',
              'lock_version': version,
              'activities': [
                {'id': 1, 'type': 'Ride', 'name': 'Ride'},
              ],
              'items': [
                {'item_type': 'activity', 'activity_id': 1},
              ],
            });
          }
          if (path == '/api/geo/project/low-res') {
            return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
          }
          if (path == '/api/geo/project/simplified') return _json(_geo(version));
          if (path == '/api/geo/project') {
            final v = version;
            fullRequests++;
            await gate.future;
            return _json(_geo(v));
          }
          if (req.method != 'GET') {
            version++; // a write
            return _json(<String, dynamic>{});
          }
          return _json(<String, dynamic>{});
        }),
      );
}

Future<bool> _waitFor(bool Function() cond) async {
  final deadline = DateTime.now().add(const Duration(seconds: 2));
  while (DateTime.now().isBefore(deadline)) {
    if (cond()) return true;
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
  return cond();
}

void main() {
  /// What reached the disk store: each row with full-resolution geometry.
  late List<Map<String, dynamic>> fullGeoRows;

  setUp(() {
    projectDataCache.resetForTest();
    fullGeoRows = [];
    projectDataCache.diskWrite = (key, row) async {
      if (row['fullGeo'] != null) fullGeoRows.add(row);
    };
  });

  /// Opens the trip and waits for the load's own seed to have written.
  Future<(ProjectNotifier, _Server)> openSeeded() async {
    final server = _Server();
    api = server.api();
    final n = ProjectNotifier(ProjectService())
      ..loadRetryBackoff = const []
      ..setMapZoom(9);
    await n.load(_ref);
    expect(await _waitFor(() => fullGeoRows.length == 1), isTrue,
        reason: 'the load seeds version 1');
    return (n, server);
  }

  int vOf(Map<String, dynamic> row) =>
      (row['fullGeo']['features'] as List).single['properties']['v'] as int;

  test('an edit refills the disk row for the new version, and only there',
      () async {
    final (n, server) = await openSeeded();

    await n.resetActivityTrack(1);
    expect(server.version, 2);
    expect(await _waitFor(() => fullGeoRows.length == 2), isTrue);

    expect(fullGeoRows.last['lockVersion'], 2);
    expect(vOf(fullGeoRows.last), 2);
    expect(await projectDataCache.readFullGeo(_ref), isNull,
        reason: 'the seed is disk only, never L1');
    n.dispose();
  });

  test('a saved track edit refills it too', () async {
    final (n, server) = await openSeeded();

    await n.saveActivityTrack(1, {'points': <dynamic>[]});
    expect(server.version, 2);
    expect(await _waitFor(() => fullGeoRows.length == 2), isTrue);

    expect(fullGeoRows.last['lockVersion'], 2);
    expect(vOf(fullGeoRows.last), 2);
    n.dispose();
  });

  test('two quick edits run two seeds and the disk ends at the newest',
      () async {
    final (n, server) = await openSeeded();
    server.gate = Completer<void>(); // hold the post-edit fetches

    await n.resetActivityTrack(1);
    expect(await _waitFor(() => server.fullRequests == 2), isTrue,
        reason: 'the first edit started a seed');
    await n.resetActivityTrack(1); // during it
    expect(server.version, 3);
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(server.fullRequests, 2, reason: 'one in flight per trip');

    server.gate.complete();
    expect(await _waitFor(() => fullGeoRows.length == 2), isTrue);
    await Future<void>.delayed(const Duration(milliseconds: 50));

    expect(server.fullRequests, 3, reason: 'run once more, not once per edit');
    expect(fullGeoRows, hasLength(2), reason: 'the stale answer was refused');
    expect(fullGeoRows.last['lockVersion'], 3);
    expect(vOf(fullGeoRows.last), 3);
    n.dispose();
  });
}
