// The offline seed never stores geometry older than the trip's version
// (issue #379, U10 escalation).
//
// The seed fetches the full-resolution geometry in the background after a
// trip opens (issue #317) and writes it to disk for offline opens. An edit
// made while that fetch is in flight bumps the trip's lock_version, and the
// edit's reload records the new version. The seed's answer, from before the
// edit, used to be written under that new version — so no later /meta could
// tell it was stale, and every offline open drew the trip as it was before
// the edit. The reload after an edit used to write full-resolution geometry
// over it in some orderings; since #379 it no longer fetches any.

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
/// waits on [releaseFull] and answers with the version it saw on arrival.
class _Server {
  int version = 1;
  int fullRequests = 0;
  final releaseFull = Completer<void>();

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
            await releaseFull.future;
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

  /// Opens the trip and waits until the seed's fetch is in flight.
  Future<(ProjectNotifier, _Server)> openWithSeedInFlight() async {
    final server = _Server();
    api = server.api();
    final n = ProjectNotifier(ProjectService())
      ..loadRetryBackoff = const []
      ..setMapZoom(9);
    await n.load(_ref);
    expect(await _waitFor(() => server.fullRequests == 1), isTrue,
        reason: 'the seed is fetching');
    return (n, server);
  }

  test('a seed whose fetch started before an edit never stores its geometry '
      'under the new version', () async {
    final (n, server) = await openWithSeedInFlight();

    await n.resetActivityTrack(1); // the edit, and its reload's /meta
    expect(server.version, 2);
    server.releaseFull.complete();
    await Future<void>.delayed(const Duration(milliseconds: 50));

    // The edit's own reload seeds afresh once the stale seed is done
    // (I2-R1-1), so a row for version 2 is expected — of version 2's geometry.
    expect(await _waitFor(() => fullGeoRows.isNotEmpty), isTrue);
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(fullGeoRows, hasLength(1),
        reason: "the stale seed's own write was refused");
    expect(fullGeoRows.single['lockVersion'], 2);
    expect((fullGeoRows.single['fullGeo']['features'] as List).single
        ['properties']['v'], 2,
        reason: 'geometry from version 1 must not be stored for version 2');
    n.dispose();
  });

  test('a seed with an unchanged version still writes', () async {
    final (n, server) = await openWithSeedInFlight();

    server.releaseFull.complete();
    expect(await _waitFor(() => fullGeoRows.isNotEmpty), isTrue);

    expect(fullGeoRows.single['lockVersion'], 1);
    expect((fullGeoRows.single['fullGeo']['features'] as List).single
        ['properties']['v'], 1);
    n.dispose();
  });
}
