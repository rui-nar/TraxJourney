// A background reload belongs to the trip it was started for (I1-R5-1 and
// I1-R5-2 of docs/reviews/CLIENT_STATE_MAP_PLAN.md, issue #278).
//
// A save on trip T reloads T when the server answers. When the user opened
// trip X first, that reload — begun before load(X) and landing after it, or
// begun only once the save came back — set the open trip back to T: T's
// items in X's panel, and every save after it addressed to T.

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

List<String> _ids(ProjectNotifier n) =>
    [for (final i in n.itemsFacet.items) (i['segment'] as Map)['id'] as String];

const _emptyGeo = {'type': 'FeatureCollection', 'features': <dynamic>[]};

/// T's details hold [tItems]; X's hold one segment. Once [holdT] is set, T's
/// details wait on [heldT]; [sort] holds the sort request until completed.
class _Server extends ProjectService {
  List<String> tItems = ['T-reloaded'];
  bool holdT = false;
  final heldT = Completer<void>();
  bool tRequested = false;
  Completer<void>? sort;

  Map<String, dynamic> _details(ProjectRef ref) => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': [
          for (final id in ref.name == 'T' ? tItems : ['X-seg']) _segment(id),
        ],
      };

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    if (ref.name == 'T') {
      tRequested = true;
      if (holdT) await heldT.future;
    }
    return _details(ref);
  }

  @override
  Future<void> sortItems(ProjectRef ref) async => sort?.future;

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

/// T is open with two segments.
_Notifier _openOnT(_Server server) => _Notifier(server)
  ..ref = _t
  ..itemsFacetWriter.setItems([_segment('T-1'), _segment('T-2')]);

/// Signs in with a server whose [method] request waits on the returned
/// completer; every other request answers `{}` at once. [sent] is completed
/// when the held request goes out.
Completer<http.Response> _holdRequest(String method, Completer<void> sent) {
  final held = Completer<http.Response>();
  signInAs(1, httpClient: MockClient((req) {
    if (req.method != method) return Future.value(http.Response('{}', 200));
    if (!sent.isCompleted) sent.complete();
    return held.future;
  }));
  return held;
}

void _expectOnX(ProjectNotifier n) {
  expect(n.ref?.name, 'X', reason: 'the user opened X; T must not come back');
  expect(_ids(n), ['X-seg']);
  expect(n.error, isNull);
}

void main() {
  setUp(() {
    projectDataCache.resetForTest();
    resetInFlightFetches();
  });

  group('a full reload (I1-R5-1)', () {
    test('held at the server when X opens, it does not apply', () async {
      signInAs(1,
          httpClient: MockClient((_) async => http.Response('{}', 200)));
      final server = _Server()..holdT = true;
      final n = _openOnT(server);

      final remove = n.removeItem(0);
      while (!server.tRequested) {
        await pumpEventQueue();
      }
      await n.load(_x);
      server.heldT.complete();
      await remove;
      await pumpEventQueue();

      _expectOnX(n);
      n.dispose();
    });

    test('a delete coming back after X opened starts no reload', () async {
      final sent = Completer<void>();
      final delete = _holdRequest('DELETE', sent);
      final server = _Server();
      final n = _openOnT(server);

      final remove = n.removeItem(0);
      await sent.future;
      await n.load(_x);
      delete.complete(http.Response('{}', 200));
      await remove;
      await pumpEventQueue();

      _expectOnX(n);
      expect(server.tRequested, isFalse,
          reason: 'a reload for the trip left is not even sent');
      n.dispose();
    });

    test('on the same trip it still applies', () async {
      signInAs(1,
          httpClient: MockClient((_) async => http.Response('{}', 200)));
      final server = _Server()..tItems = ['T-2'];
      final n = _openOnT(server);
      var notified = 0;
      // Both change the item list, which the content facet's listeners hear
      // of (#294).
      n.itemsFacet.addListener(() => notified++);

      await n.removeItem(0);

      expect(n.ref?.name, 'T');
      expect(_ids(n), ['T-2']);
      expect(server.tRequested, isTrue);
      expect(notified, greaterThanOrEqualTo(2),
          reason: 'the local removal, then the reload');
      expect(n.error, isNull);
      n.dispose();
    });
  });

  group('a details-only reload begun after X opened (I1-R5-2)', () {
    test('a memory created on T stays off X', () async {
      final sent = Completer<void>();
      final post = _holdRequest('POST', sent);
      final server = _Server();
      final n = _openOnT(server);

      final create = n.createMemory(date: '2026-01-01', geoMode: 'none');
      await sent.future;
      await n.load(_x);
      post.complete(http.Response('{}', 200));
      await create;
      await pumpEventQueue();

      _expectOnX(n);
      expect(server.tRequested, isFalse);
      n.dispose();
    });

    test('a person created on T stays off X', () async {
      final sent = Completer<void>();
      final post = _holdRequest('POST', sent);
      final server = _Server();
      final n = _openOnT(server);

      final create = n.createPerson(name: 'Ana');
      await sent.future;
      await n.load(_x);
      post.complete(http.Response(jsonEncode({'id': 7}), 200));
      await create;
      await pumpEventQueue();

      _expectOnX(n);
      expect(server.tRequested, isFalse);
      n.dispose();
    });

    test('a sort of T stays off X', () async {
      signInAs(1,
          httpClient: MockClient((_) async => http.Response('{}', 200)));
      final server = _Server()..sort = Completer<void>();
      final n = _openOnT(server);

      final sort = n.sortItemsByDate();
      await pumpEventQueue();
      await n.load(_x);
      server.sort!.complete();
      await sort;
      await pumpEventQueue();

      _expectOnX(n);
      expect(server.tRequested, isFalse);
      n.dispose();
    });

    test('a reorder of T stays off X', () async {
      final sent = Completer<void>();
      final put = _holdRequest('PUT', sent);
      final server = _Server();
      final n = _openOnT(server);

      final reorder = n.reorderItems(0, 1);
      await sent.future;
      await n.load(_x);
      put.complete(http.Response('{}', 200));
      await reorder;
      await pumpEventQueue();

      _expectOnX(n);
      expect(server.tRequested, isFalse);
      n.dispose();
    });

    test('on the same trip it still applies', () async {
      signInAs(1,
          httpClient: MockClient((_) async => http.Response('{}', 200)));
      final server = _Server()..tItems = ['T-2', 'T-1'];
      final n = _openOnT(server);

      await n.reorderItems(0, 1);

      expect(n.ref?.name, 'T');
      expect(_ids(n), ['T-2', 'T-1']);
      expect(server.tRequested, isTrue);
      expect(n.error, isNull);
      n.dispose();
    });
  });
}
