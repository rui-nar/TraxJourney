// The geometry facet records the level of detail its geometry was built for,
// and orders server answers by the start of the request that produced them
// (issues #294 and #379; Decisions 10, 21 and 24 of
// docs/CLIENT_STATE_MAP_PLAN.md).

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/facets/project_facet.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');
const _box = GeoBox(7.0, 45.0, 8.0, 46.0);

Map<String, dynamic> _collection(String tag) => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': '1', 'tag': tag},
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

String? _tag(GeoFacet f) =>
    ((f.geo?['features'] as List?)?.first as Map?)?['properties']['tag']
        as String?;

void main() {
  setUp(() => projectDataCache.resetForTest());

  group('GeoLod', () {
    test('a level carries its bucket and box; the others carry neither', () {
      const level = GeoLod.level(9, box: _box);
      expect(level.kind, GeoLodKind.level);
      expect((level.bucket, level.box), (9, _box));
      for (final lod in [GeoLod.none, GeoLod.lowRes, GeoLod.full]) {
        expect((lod.bucket, lod.box), (null, null), reason: '$lod');
      }
    });

    test('equal by value', () {
      expect(const GeoLod.level(9, box: GeoBox(7.0, 45.0, 8.0, 46.0)),
          const GeoLod.level(9, box: _box));
      expect(const GeoLod.level(9), isNot(const GeoLod.level(9, box: _box)));
      expect(const GeoLod.level(9), isNot(const GeoLod.level(10)));
      expect(GeoLod.full, isNot(GeoLod.lowRes));
    });
  });

  group('GeoFacetWriter', () {
    late GeoFacetWriter w;
    setUp(() => w = GeoFacetWriter());
    tearDown(() => w.dispose());

    test('starts empty, with nothing loaded yet to wait for', () {
      expect(w.facet.geo, isNull);
      expect(w.facet.lod, GeoLod.none);
      expect(w.facet.servedFrom, 0);
      expect(w.facet.isLoaded, isTrue);
      expect(w.facet.version, 0);
    });

    test('replace writes the geometry and its level together', () {
      final geo = _collection('a');
      expect(w.replace(geo, const GeoLod.level(9, box: _box), servedFrom: 3),
          isTrue);
      expect(w.facet.geo, same(geo));
      expect(w.facet.lod, const GeoLod.level(9, box: _box));
      expect(w.facet.servedFrom, 3);
      expect(w.facet.version, 1);
    });

    test('an answer not newer than the one on screen is ignored (Decision 24)',
        () {
      w.replace(_collection('newer'), const GeoLod.level(9), servedFrom: 5);

      for (final stamp in [5, 4]) {
        expect(
            w.replace(_collection('older'), const GeoLod.level(12),
                servedFrom: stamp),
            isFalse,
            reason: 'started at $stamp');
      }
      expect(_tag(w.facet), 'newer');
      expect(w.facet.lod, const GeoLod.level(9));
      expect(w.facet.servedFrom, 5);
      expect(w.facet.version, 1, reason: 'an ignored answer changes nothing');

      expect(w.replace(_collection('newest'), const GeoLod.level(12),
          servedFrom: 6), isTrue);
      expect(_tag(w.facet), 'newest');
    });

    test('a write that answers no request always applies, and counts as older '
        'than any request', () {
      w.replace(_collection('server'), const GeoLod.level(9), servedFrom: 7);

      // The offline cache, a client-side build: no request behind it.
      expect(w.replace(_collection('cache'), GeoLod.full), isTrue);
      expect(_tag(w.facet), 'cache');
      expect(w.facet.servedFrom, 0);
      // So any server answer that comes after replaces it.
      expect(w.replace(_collection('later'), const GeoLod.level(9),
          servedFrom: 1), isTrue);
    });

    test('a local edit keeps the level and the request it answers', () {
      w.replace(_collection('server'), const GeoLod.level(9, box: _box),
          servedFrom: 4);
      final patched = _collection('patched');

      w.replaceKeepingLod(patched);

      expect(w.facet.geo, same(patched));
      expect(w.facet.lod, const GeoLod.level(9, box: _box));
      expect(w.facet.servedFrom, 4,
          reason: 'an answer older than the one patched is still older');
      expect(w.facet.version, 2);
    });

    test('forgetBox keeps the level and drops only its box', () {
      w.replace(_collection('a'), const GeoLod.level(9, box: _box));
      w.forgetBox();
      expect(w.facet.lod, const GeoLod.level(9));
      final v = w.facet.version;
      w.forgetBox(); // nothing left to drop
      expect(w.facet.version, v);
    });

    test('setLoaded marks the facet only when the flag changes', () {
      w.setLoaded(true);
      expect(w.facet.version, 0);
      w.setLoaded(false);
      expect((w.facet.isLoaded, w.facet.version), (false, 1));
    });

    test('reset puts back the state before any load', () {
      w
        ..replace(_collection('a'), const GeoLod.level(9, box: _box),
            servedFrom: 8)
        ..setLoaded(false)
        ..reset();
      expect(w.facet.geo, isNull);
      expect(w.facet.lod, GeoLod.none);
      expect(w.facet.servedFrom, 0);
      expect(w.facet.isLoaded, isTrue);
    });
  });

  test("a geometry write tells the facet's listeners on the notifier's next "
      'notify, not before (Decision 17)', () {
    final n = ProjectNotifier(ProjectService());
    final heard = <String>[];
    n.geoFacet.addListener(() => heard.add('geo'));
    n.addListener(() => heard.add('root'));

    n.geoFacetWriter.replace(_collection('a'), GeoLod.lowRes);
    expect(heard, isEmpty);
    n.notifyListeners();
    expect(heard, ['geo', 'root']);
    n.dispose();
  });

  test('resetProgressiveFlags marks every phase not loaded, the geometry '
      "facet's included (P2-R1-5)", () {
    final n = _Subclass();
    n.reset();
    expect((n.isMetaLoaded, n.isElevationLoaded, n.geoFacet.isLoaded),
        (false, false, false));
    n.clear();
    expect(n.geoFacet.isLoaded, isTrue);
    n.dispose();
  });

  group('fetchServerGeo stamps an answer with the request that produced it '
      '(Decision 24, P2-R2-1)', () {
    test('a caller handed a request already in flight gets its start', () async {
      final n = ProjectNotifier(ProjectService());
      final shared = Completer<Map<String, dynamic>>();
      final own = Completer<Map<String, dynamic>>();

      final first = n.fetchServerGeo(() => shared.future);
      final joiner = n.fetchServerGeo(() => shared.future);
      final fresh = n.fetchServerGeo(() => own.future);
      shared.complete(_collection('a'));
      own.complete(_collection('b'));
      final (a, b, c) = (await first, await joiner, await fresh);

      expect(b.servedFrom, a.servedFrom,
          reason: 'the joiner gets the first request\'s answer, so its stamp');
      expect(c.servedFrom, greaterThan(b.servedFrom),
          reason: 'a request of its own is stamped when it starts');
      // The patch reconcile reading is unchanged: the oldest in flight.
      expect((b.requestedAt, c.requestedAt), (a.servedFrom, a.servedFrom));
      n.dispose();
    });

    test("through the service: a dedup join gets the joined request's start, "
        'a fresh request its own and its own HTTP request', () async {
      final released = Completer<void>();
      var requests = 0;
      api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.url.path != '/api/geo/project/simplified') {
            return http.Response('{}', 200);
          }
          requests++;
          await released.future;
          return http.Response(jsonEncode(_collection('x')), 200);
        }),
      );
      final service = ProjectService();
      // Two notifiers on one service: the view-mode and the app-wide one.
      final view = ProjectNotifier(service);
      final manage = ProjectNotifier(service);

      final started = view.fetchServerGeo(
          () => service.getSimplifiedGeo(_ref, 9, bbox: _box));
      final joined = manage.fetchServerGeo(
          () => service.getSimplifiedGeo(_ref, 9, bbox: _box));
      final fresh = manage.fetchServerGeo(
          () => service.getSimplifiedGeoFresh(_ref, 9, bbox: _box));
      await Future<void>.delayed(const Duration(milliseconds: 10));
      expect(requests, 2, reason: 'the join sends nothing; the fresh one does');

      released.complete();
      final (a, b, c) = (await started, await joined, await fresh);
      expect(b.servedFrom, a.servedFrom);
      expect(c.servedFrom, greaterThan(a.servedFrom));
      view.dispose();
      manage.dispose();
    });
  });

  group('the export decides "already full" by the level of detail', () {
    late int fullFetches;
    late ProjectNotifier n;

    setUp(() {
      fullFetches = 0;
      api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.url.path == '/api/geo/project') {
            fullFetches++;
            return http.Response(jsonEncode(_collection('full')), 200);
          }
          return http.Response('{}', 200);
        }),
      );
      n = ProjectNotifier(ProjectService())..ref = _ref;
    });
    tearDown(() => n.dispose());

    test('a zoom level is upgraded to full resolution', () async {
      n.geoFacetWriter.replace(_collection('level'), const GeoLod.level(9));
      expect(_tagOf(await n.fullResGeoForExport()), 'full');
      expect(fullFetches, 1);
      expect(_tag(n.geoFacet), 'level', reason: 'the map keeps its level');
    });

    for (final lod in [GeoLod.full, GeoLod.lowRes]) {
      test('$lod is exported as it is', () async {
        final geo = _collection('on screen');
        n.geoFacetWriter.replace(geo, lod);
        expect(await n.fullResGeoForExport(), same(geo));
        expect(fullFetches, 0);
      });
    }
  });
}

String? _tagOf(Map<String, dynamic>? geo) =>
    ((geo?['features'] as List?)?.first as Map?)?['properties']['tag']
        as String?;

/// Calls the protected [resetProgressiveFlags] the way ViewProjectNotifier and
/// SharedProjectNotifier do.
class _Subclass extends ProjectNotifier {
  _Subclass() : super(ProjectService());

  void reset() => resetProgressiveFlags();
}
