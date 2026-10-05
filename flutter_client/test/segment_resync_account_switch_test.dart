// A 409 resync's geometry belongs to the account that hit the conflict
// (I1-R3-1 of docs/reviews/CLIENT_STATE_MAP_PLAN.md, issue #418).
//
// The resync only checked that the open trip had the same name and owner, and
// an own trip has no owner: once A signed out and B opened B's own trip of the
// same name, A's full-res geometry landing late was drawn as B's.

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'helpers/signed_in.dart';

const _trip = ProjectRef(name: 'Trip');

Map<String, dynamic> _geo(double lat) => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [0.0, lat],
              [1.0, lat],
            ],
          },
          'properties': {'type': 'segment', 'segment_id': 's1'},
        },
      ],
    };

Map<String, dynamic> get _segmentItem => {
      'item_type': 'segment',
      'segment': {
        'id': 's1',
        'segment_type': 'flight',
        'label': 'seg',
        'start': {'lat': 1.0, 'lon': 0.0},
        'end': {'lat': 1.0, 'lon': 1.0},
      },
    };

/// The resync's full-res geo request is held on [heldGeo].
class _Server extends ProjectService {
  final heldGeo = Completer<Map<String, dynamic>>();
  int geoCalls = 0;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': [_segmentItem],
      };

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
      {bool bypassCache = false}) {
    geoCalls++;
    return heldGeo.future;
  }
}

/// Signs in as [userId] the way AuthNotifier does: token, cache scope, and the
/// app-wide notifier's account check.
void _session(ProjectNotifier n, int? userId) {
  if (userId != null) {
    signInAs(userId,
        httpClient: MockClient((req) async => req.method == 'PUT'
            ? http.Response(jsonEncode({'detail': 'conflict'}), 409)
            : http.Response('{}', 200)));
  }
  projectDataCache.setCurrentUser(userId);
  n.onAuthChanged(userId?.toString());
}

void main() {
  setUp(() => projectDataCache.resetForTest());

  test("a resync landing after a sign-out leaves the next account's "
      'same-named trip alone', () async {
    final server = _Server();
    final n = ProjectNotifier(server);
    _session(n, 1);
    n
      ..ref = _trip
      ..items = [_segmentItem]
      ..geo = _geo(1);

    final save = n.updateSegment('s1',
        segmentType: 'flight',
        label: 'seg',
        startLat: 2,
        startLon: 0,
        endLat: 2,
        endLon: 1);
    // The PUT gets its 409 and the resync's geo request goes out.
    while (server.geoCalls == 0) {
      await Future<void>.delayed(Duration.zero);
    }

    // A signs out; B signs in and opens B's own trip, also called "Trip".
    _session(n, null);
    _session(n, 2);
    final bGeo = _geo(5);
    n
      ..ref = _trip
      ..items = [_segmentItem]
      ..geo = bGeo;

    server.heldGeo.complete(_geo(9)); // A's geometry
    await save;

    expect(n.geo, same(bGeo), reason: "A's geometry must not land on B's trip");
    expect(n.error, isNull, reason: "A's conflict message is not B's");
    n.dispose();
  });
}
