// ProjectService's read-through/write-through wiring to projectDataCache —
// the mechanism that lets switching between view mode and manage mode reuse
// data the other mode already fetched instead of re-downloading it. See
// project_data_cache.dart for the full design.

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

http.Response _json(Map<String, dynamic> body) =>
    http.Response(jsonEncode(body), 200);

void main() {
  setUp(() => projectDataCache.resetForTest());

  test('getDetailsMeta always hits the network, even on a repeat call',
      () async {
    var calls = 0;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          calls++;
          return _json({'lock_version': 1, 'name': 'Trip'});
        }));
    final svc = ProjectService();

    await svc.getDetailsMeta(_ref);
    await svc.getDetailsMeta(_ref);

    expect(calls, 2, reason: 'meta is the freshness oracle — it must never be served from cache');
  });

  test('getGeo is served from cache on a repeat call once meta confirms the '
      'project has not changed', () async {
    var geoCalls = 0;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.url.path.contains('/geo/')) {
            geoCalls++;
            return _json({'type': 'FeatureCollection', 'features': []});
          }
          return _json({'lock_version': 1, 'name': 'Trip'});
        }));
    final svc = ProjectService();

    await svc.getDetailsMeta(_ref); // establishes lock_version 1
    await svc.getGeo(_ref);
    await svc.getDetailsMeta(_ref); // repeat meta — still lock_version 1
    await svc.getGeo(_ref); // should be served from projectDataCache

    expect(geoCalls, 1);
  });

  test('a lock_version change invalidates the cached geo', () async {
    var geoCalls = 0;
    var version = 1;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.url.path.contains('/geo/')) {
            geoCalls++;
            return _json({'type': 'FeatureCollection', 'features': []});
          }
          return _json({'lock_version': version, 'name': 'Trip'});
        }));
    final svc = ProjectService();

    await svc.getDetailsMeta(_ref);
    await svc.getGeo(_ref);
    version = 2; // simulates a mutation landing on the server meanwhile
    await svc.getDetailsMeta(_ref);
    await svc.getGeo(_ref);

    expect(geoCalls, 2, reason: 'a changed lock_version must force a re-fetch');
  });

  test('bypassCache always hits the network, cache hit or not', () async {
    var detailsCalls = 0;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.url.path == '/api/projects/Trip') {
            detailsCalls++;
            return _json({'name': 'Trip', 'activities': []});
          }
          return _json({'lock_version': 1, 'name': 'Trip'});
        }));
    final svc = ProjectService();

    await svc.getDetailsMeta(_ref);
    await svc.getDetails(_ref); // populates the cache
    await svc.getDetails(_ref, bypassCache: true); // must not be served from cache

    expect(detailsCalls, 2);
  });

  test('view mode and manage mode share the same full-details cache slot',
      () async {
    // Regression test for the reported bug: switching from view mode to
    // manage mode (or back) re-downloaded the ~12 MB full-details payload
    // even though the other mode had just fetched it. Both modes go through
    // plain ProjectService.getDetails() — view mode's own fetchFullDetails()
    // writes into the very same projectDataCache slot (see view_screen.dart).
    var detailsCalls = 0;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.url.path == '/api/projects/Trip') {
            detailsCalls++;
            return _json({'name': 'Trip', 'activities': []});
          }
          return _json({'lock_version': 1, 'name': 'Trip'});
        }));

    await ProjectService().getDetailsMeta(_ref);
    await ProjectService().getDetails(_ref); // e.g. manage mode's background elevation fetch
    await ProjectService().getDetails(_ref); // e.g. view mode's fetchFullDetails, moments later

    expect(detailsCalls, 1);
  });

  // Regression tests for the Android ANR reported when toggling from view mode
  // to manage mode *while the trip is still loading*: the mode being left has
  // its full-details/full-geo fetch in flight, and the mode being entered
  // starts load() from scratch before the first fetch has populated
  // projectDataCache — so, before this fix, both issued the same multi-MB
  // request at once and both landed a synchronous jsonDecode on the UI isolate
  // back to back. Each test below gates the mock response on a Completer so
  // both calls are genuinely in flight together (not just sequential), which
  // a plain cache-after-completion test can't exercise.

  test('two concurrent getDetails calls for the same ref only hit the network once',
      () async {
    var detailsCalls = 0;
    final gate = Completer<void>();
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          detailsCalls++;
          await gate.future;
          return _json({'name': 'Trip', 'activities': []});
        }));

    // Two independent ProjectService instances — mirrors manage mode's
    // long-lived singleton vs. view mode's fresh instance per toggle.
    final f1 = ProjectService().getDetails(_ref);
    final f2 = ProjectService().getDetails(_ref);
    gate.complete();
    final results = await Future.wait([f1, f2]);

    expect(detailsCalls, 1,
        reason: 'the second caller should await the first fetch instead of '
            'starting its own');
    expect(results[0], same(results[1]));
  });

  test('two concurrent getGeo calls for the same ref only hit the network once',
      () async {
    var geoCalls = 0;
    final gate = Completer<void>();
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          geoCalls++;
          await gate.future;
          return _json({'type': 'FeatureCollection', 'features': []});
        }));

    final f1 = ProjectService().getGeo(_ref);
    final f2 = ProjectService().getGeo(_ref);
    gate.complete();
    await Future.wait([f1, f2]);

    expect(geoCalls, 1);
  });

  test('two concurrent getLowResGeo calls for the same ref only hit the network once',
      () async {
    var geoCalls = 0;
    final gate = Completer<void>();
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          geoCalls++;
          await gate.future;
          return _json({'type': 'FeatureCollection', 'features': []});
        }));

    final f1 = ProjectService().getLowResGeo(_ref);
    final f2 = ProjectService().getLowResGeo(_ref);
    gate.complete();
    await Future.wait([f1, f2]);

    expect(geoCalls, 1);
  });

  test(
      'a dedup key is reused after the in-flight fetch settles, so a later '
      'call fetches again rather than replaying a stale result', () async {
    var detailsCalls = 0;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          detailsCalls++;
          return _json({'name': 'Trip', 'activities': [], 'call': detailsCalls});
        }));

    await ProjectService().getDetails(_ref, bypassCache: true);
    await ProjectService().getDetails(_ref, bypassCache: true);

    expect(detailsCalls, 2);
  });

  test(
      'a rejected dedup-tracked fetch does not surface as an orphaned '
      'unhandled async error', () async {
    // Regression test: _dedupFetch's whenComplete() cleanup hook creates its
    // own derived Future carrying the same outcome as the fetch it's
    // observing. That derived Future going unlistened-to meant a failed
    // fetch was reported as an unhandled async error moments after the
    // caller had already caught the very same rejection via the Future
    // _dedupFetch returns — exactly the failure mode
    // project_notifier_load_test.dart guards against for load()'s own
    // futures. If this regresses, flutter_test fails this test with
    // "<exception> was thrown ... but after the test had completed" even
    // though every assertion below still passes.
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async => http.Response('nope', 400)));

    await expectLater(ProjectService().getGeo(_ref), throwsA(anything));

    // Give whenComplete()'s derived Future a chance to reject unobserved,
    // the way it used to.
    await Future<void>.delayed(const Duration(milliseconds: 20));
  });
}
