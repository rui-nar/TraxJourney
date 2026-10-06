// One ProjectNotifier per signed-in account (issue #418; I1-R4-1 and I1-R4-2
// of docs/reviews/CLIENT_STATE_MAP_PLAN.md).
//
// The app-wide notifier used to serve every account, cleared at each account
// change. Any await inside it let a response started under account A land
// after A signed out and B signed in — and a trip is known by name and owner,
// with no owner for an own trip, so the response passed every trip check on
// B's trip of the same name. Each round of review found another such path.
// Now an account change replaces the notifier: what A started lands in A's
// discarded instance, whatever the path.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/main.dart' show accountScopedProjectNotifier;
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'helpers/signed_in.dart';

const _japan = ProjectRef(name: 'Japan');
const _day = '2024-04-01';

/// A segment of A's Japan, the one A edits.
Map<String, dynamic> _segment({String status = 'resolved'}) => {
      'item_type': 'segment',
      'segment': {
        'id': 's1',
        'segment_type': 'train',
        'label': 'A train',
        'date': _day,
        'route_status': status,
        'start': {'lat': 1.0, 'lon': 0.0},
        'end': {'lat': 1.0, 'lon': 1.0},
      },
    };

const _emptyGeo = {'type': 'FeatureCollection', 'features': <dynamic>[]};

/// Each account's own "Japan", told apart by the token it is asked under:
/// A's has a segment and A's note, B's neither.
class _Service extends ProjectService {
  _Service({this.pendingSegment = false});

  /// A's segment is still resolving, so a load starts the resolve poller.
  final bool pendingSegment;

  /// Every request this service has answered.
  int calls = 0;

  bool get _isA => api.tokenUserId == 1;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    calls++;
    return {
      'name': ref.name,
      'activities': <dynamic>[],
      'items': [
        if (_isA) _segment(status: pendingSegment ? 'pending' : 'resolved'),
      ],
      'people': <dynamic>[],
      'groups': <dynamic>[],
      'day_meta': {
        _day: {'note': _isA ? 'A note' : 'B note'},
      },
    };
  }

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) =>
      getDetailsMeta(ref);

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async {
    calls++;
    return _emptyGeo;
  }

  @override
  Future<Map<String, dynamic>> getSimplifiedGeo(ProjectRef ref, double zoom,
      {GeoBox? bbox}) async {
    calls++;
    return _emptyGeo;
  }

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
      {bool bypassCache = false}) async {
    calls++;
    return _emptyGeo;
  }

  @override
  Future<Map<String, List<String>>> getMemoryPhotos(ProjectRef ref) async {
    calls++;
    return const {};
  }
}

class _Notifier extends ProjectNotifier {
  _Notifier(_Service super.service) : server = service;

  final _Service server;

  @override
  bool get loadOwnerExtras => false;
}

class _AuthService extends AuthService {
  @override
  Future<void> appOpened(String sessionState) async {}

  @override
  Future<Map<String, dynamic>> loginWithPassword(String email, String pw) async {
    final id = int.parse(email);
    api.setToken(fakeJwt(sub: id));
    return {'id': id, 'email': '$id@example.com', 'auth_provider': 'local'};
  }

  @override
  Future<void> logout() async => api.clearToken();
}

/// Answers every request with an empty object, except the one [hold] names,
/// which waits on [held].
class _Http {
  _Http(this.hold);

  final bool Function(http.Request) hold;
  final held = Completer<http.Response>();
  bool sent = false;

  http.Client get client => MockClient((req) {
        if (hold(req)) {
          sent = true;
          return held.future;
        }
        return Future.value(http.Response('{}', 200));
      });
}

class _App {
  _App(this.tester, {bool pendingSegment = false})
      : auth = AuthNotifier(_AuthService()),
        _pendingSegment = pendingSegment;

  final WidgetTester tester;
  final AuthNotifier auth;
  final bool _pendingSegment;
  late BuildContext _context;

  /// What the app's screens get from the provider now.
  _Notifier get notifier =>
      Provider.of<ProjectNotifier>(_context, listen: false) as _Notifier;

  Future<void> pump() async {
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthNotifier>.value(value: auth),
        accountScopedProjectNotifier(
            () => _Notifier(_Service(pendingSegment: _pendingSegment))),
      ],
      child: Builder(builder: (context) {
        _context = context;
        return const SizedBox();
      }),
    ));
  }

  Future<void> signIn(int id) async {
    await tester.runAsync(() => auth.loginWithPassword('$id', 'pw'));
    await tester.pump();
  }

  Future<void> signOut() async {
    await tester.runAsync(auth.logout);
    await tester.pump();
  }
}

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    projectDataCache.resetForTest();
    api = ApiClient();
  });

  testWidgets(
      'an account change hands out a new notifier and disposes the old one, '
      'with no timer left running', (tester) async {
    final app = _App(tester, pendingSegment: true);
    await app.pump();
    await app.signIn(1);
    final a = app.notifier;

    // A's trip, loaded with everything that runs on a timer going: the
    // resolve poller (A's segment is resolving), photo polling, the
    // degraded-route watch, a zoom refetch waiting on the camera, and the
    // debounce of the next one.
    a.load(_japan);
    for (var i = 0; i < 20 && a.server.calls < 3; i++) {
      await tester.pump(const Duration(milliseconds: 10));
    }
    await tester.pump();
    expect(a.isLoading, isFalse);
    a
      ..startPhotoPolling(_japan)
      ..startDegradedRouteWatch(_japan)
      ..setMapCameraActive(true)
      ..setMapZoom(18);
    await tester.pump(a.zoomRefetchDebounce + const Duration(milliseconds: 10));
    a.setMapZoom(19);

    await app.signOut();

    expect(app.notifier, isNot(same(a)));
    expect(a.isAlive, isFalse, reason: 'the provider disposes the one it drops');
    expect(app.notifier.ref, isNull);

    // No time passes: every timer A armed above is still due. The new
    // notifier goes with the tree, and the test binding then fails the test
    // on any timer still pending — one A's dispose() left running.
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets(
      "a day note's PATCH landing after the account changed leaves the next "
      "account's trip of the same name alone (I1-R4-1)", (tester) async {
    final server = _Http((req) => req.method == 'PATCH');
    api = ApiClient(httpClient: server.client);
    final app = _App(tester);
    await app.pump();
    await app.signIn(1);

    final a = app.notifier;
    late Future<void> saving;
    await tester.runAsync(() async {
      await a.load(_japan);
      expect(a.dayMeta[_day], {'note': 'A note'});
      saving = a.saveDayMeta(days: {
        _day: {'note': 'A edit'},
      });
      while (!server.sent) {
        await pumpEventQueue();
      }
    });

    // A signs out; B signs in and opens B's own Japan.
    await app.signOut();
    await app.signIn(2);
    final b = app.notifier;
    await tester.runAsync(() => b.load(_japan));
    expect(b.dayMeta[_day], {'note': 'B note'});

    await tester.runAsync(() async {
      server.held.complete(http.Response(
          jsonEncode({
            'day_meta': {
              _day: {'note': 'A edit'},
            },
          }),
          200));
      await saving;
    });
    await tester.pump();

    expect(app.notifier.dayMeta[_day], {'note': 'B note'},
        reason: "A's notes must not land on B's trip");
  });

  testWidgets(
      "a segment edit's PUT landing after the account changed draws nothing "
      "on the next account's map (I1-R4-2)", (tester) async {
    final server = _Http((req) => req.method == 'PUT');
    api = ApiClient(httpClient: server.client);
    final app = _App(tester);
    await app.pump();
    await app.signIn(1);

    final a = app.notifier;
    late Future<void> saving;
    await tester.runAsync(() async {
      await a.load(_japan);
      saving = a.updateSegment('s1',
          segmentType: 'train',
          label: 'A train',
          startLat: 2,
          startLon: 0,
          endLat: 2,
          endLon: 1,
          routeMode: 'great_circle');
      while (!server.sent) {
        await pumpEventQueue();
      }
    });

    await app.signOut();
    await app.signIn(2);
    final b = app.notifier;
    await tester.runAsync(() => b.load(_japan));

    await tester.runAsync(() async {
      server.held.complete(http.Response('{}', 200));
      await saving;
    });
    await tester.pump();

    final features = app.notifier.geo?['features'] as List? ?? const [];
    expect(
        features.where((f) => (f as Map)['properties']?['segment_id'] == 's1'),
        isEmpty,
        reason: "A's segment must not be drawn on B's map");
    expect(app.notifier.mergePendingSegmentPatches([]), isEmpty,
        reason: "nor kept as a patch for B's next geometry");
  });
}
