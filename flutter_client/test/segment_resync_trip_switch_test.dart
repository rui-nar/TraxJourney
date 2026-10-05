// A 409 resync belongs to the trip that hit the conflict (I1-R4-3 of
// docs/reviews/CLIENT_STATE_MAP_PLAN.md, issue #278).
//
// The resync reloads the trip's details, and that reload sets the open trip.
// Started for trip T after the user had opened X, or landing after X was
// opened, it put the notifier back on T: T's items in X's panel, and every
// save after it addressed to T.

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'helpers/signed_in.dart';

const _t = ProjectRef(name: 'T');
const _x = ProjectRef(name: 'X');

Map<String, dynamic> _segment(String id) => {
      'item_type': 'segment',
      'segment': {
        'id': id,
        'segment_type': 'flight',
        'label': id,
        'start': {'lat': 1.0, 'lon': 0.0},
        'end': {'lat': 1.0, 'lon': 1.0},
      },
    };

const _emptyGeo = {'type': 'FeatureCollection', 'features': <dynamic>[]};

/// Each trip has one segment named after it. T's details, once [holdT] is
/// set, wait on [heldT].
class _Server extends ProjectService {
  bool holdT = false;
  final heldT = Completer<Map<String, dynamic>>();
  bool tRequested = false;

  Map<String, dynamic> _details(ProjectRef ref) => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': [_segment('${ref.name}-seg')],
      };

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    if (ref.name == 'T' && holdT) {
      tRequested = true;
      return heldT.future;
    }
    return _details(ref);
  }

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async => _emptyGeo;

  @override
  Future<Map<String, dynamic>> getSimplifiedGeo(ProjectRef ref, double zoom,
          {GeoBox? bbox}) async =>
      _emptyGeo;

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _emptyGeo;
}

class _Notifier extends ProjectNotifier {
  _Notifier(super.service);

  @override
  bool get loadOwnerExtras => false;
}

/// T is open with its segment, and an edit to it is sent.
Future<void> _editOnT(ProjectNotifier n) {
  n
    ..ref = _t
    ..items = [_segment('T-seg')];
  return n.updateSegment('T-seg',
      segmentType: 'flight',
      label: 'edited',
      startLat: 2,
      startLon: 0,
      endLat: 2,
      endLon: 1);
}

void _expectOnX(ProjectNotifier n) {
  expect(n.ref?.name, 'X', reason: 'the user opened X; T must not come back');
  expect(n.items.map((i) => (i['segment'] as Map)['id']), ['X-seg']);
  expect(n.error, isNull, reason: "T's conflict message is not X's");
}

void main() {
  setUp(() {
    projectDataCache.resetForTest();
    resetInFlightFetches();
  });

  test('a conflict coming back after X was opened leaves the notifier on X',
      () async {
    final put = Completer<http.Response>();
    var putSent = false;
    signInAs(1, httpClient: MockClient((req) {
      if (req.method != 'PUT') return Future.value(http.Response('{}', 200));
      putSent = true;
      return put.future;
    }));
    final n = _Notifier(_Server());

    final save = _editOnT(n);
    while (!putSent) {
      await pumpEventQueue();
    }
    await n.load(_x);
    put.complete(http.Response(jsonEncode({'detail': 'conflict'}), 409));
    await save;
    await pumpEventQueue();

    _expectOnX(n);
    n.dispose();
  });

  test("a conflict's reload landing after X was opened leaves the notifier "
      'on X', () async {
    signInAs(1,
        httpClient: MockClient((req) async => req.method == 'PUT'
            ? http.Response(jsonEncode({'detail': 'conflict'}), 409)
            : http.Response('{}', 200)));
    final server = _Server()..holdT = true;
    final n = _Notifier(server);

    final save = _editOnT(n);
    while (!server.tRequested) {
      await pumpEventQueue();
    }
    await n.load(_x);
    server.heldT.complete(server._details(_t));
    await save;
    await pumpEventQueue();

    _expectOnX(n);
    n.dispose();
  });
}
