// Regression tests for a mutation-propagation audit of the manual
// memory-add / journal-add flows:
//
//  - Fix 1: createMemory/createJournal used to leave their optimistic
//    placeholder in `items` forever when the create request failed — the
//    catch block only set `error`. A later tap on that phantom item sent
//    requests like PUT /api/memories/__optimistic__, which 422s since the id
//    isn't real. Fixed by rolling the placeholder back on failure.
//  - Fix 2: both mixins used the fixed literal '__optimistic__' as the temp
//    id for every pending create, so two concurrent creates could produce
//    two items sharing the same id/key — the same class of bug the map-panel
//    ANR fix (map_panel_memory_marker_key_test.dart) addressed for a
//    different cause. Fixed with a per-call unique id.
//  - Fix 3: createMemory/createJournal now return a bool the caller can
//    check instead of silently swallowing the failure.
library;

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

ProjectNotifier _notifier() =>
    ProjectNotifier(ProjectService())..ref = _ref;

void main() {
  setUp(() => projectDataCache.resetForTest());

  group('createMemory', () {
    test('a failed create rolls back the optimistic placeholder and reports failure',
        () async {
      api = ApiClient(
          baseUrl: '',
          httpClient: MockClient(
              (req) async => http.Response('{"detail":"boom"}', 500)));

      final notifier = _notifier();
      final ok = await notifier.createMemory(
        date: '2026-01-01',
        geoMode: 'start_of_day',
      );

      expect(ok, isFalse);
      expect(notifier.items, isEmpty,
          reason: 'the optimistic placeholder must not survive a failed create');
      expect(notifier.error, isNotNull);
    });

    test('two concurrent creates never share a placeholder id', () async {
      final gate = Completer<void>();
      api = ApiClient(
          baseUrl: '',
          httpClient: MockClient((req) async {
            // Stall both in-flight requests so their placeholders coexist.
            await gate.future;
            return http.Response('{"detail":"boom"}', 500);
          }));

      final notifier = _notifier();
      final f1 = notifier.createMemory(date: '2026-01-01', geoMode: 'start_of_day');
      final f2 = notifier.createMemory(date: '2026-01-02', geoMode: 'start_of_day');

      // Both async calls run synchronously up to their first `await`, so by
      // this point (still before either network call resolves) both
      // placeholders are already in `items`.
      expect(notifier.items.length, 2);
      final ids = notifier.items
          .map((i) => i['memory']?['id']?.toString())
          .toSet();
      expect(ids.length, 2,
          reason: 'placeholder ids must be unique per call, or the map '
              'marker layer would see duplicate ValueKeys');

      gate.complete();
      await Future.wait([f1, f2]);
      expect(notifier.items, isEmpty); // both creates failed → both rolled back
    });
  });

  group('createJournal', () {
    test('a failed create rolls back the optimistic placeholder and reports failure',
        () async {
      api = ApiClient(
          baseUrl: '',
          httpClient: MockClient(
              (req) async => http.Response('{"detail":"boom"}', 500)));

      final notifier = _notifier();
      final ok = await notifier.createJournal(
        date: '2026-01-01',
        geoMode: 'start_of_day',
      );

      expect(ok, isFalse);
      expect(notifier.items, isEmpty,
          reason: 'the optimistic placeholder must not survive a failed create');
      expect(notifier.error, isNotNull);
    });

    test('two concurrent creates never share a placeholder id', () async {
      final gate = Completer<void>();
      api = ApiClient(
          baseUrl: '',
          httpClient: MockClient((req) async {
            await gate.future;
            return http.Response('{"detail":"boom"}', 500);
          }));

      final notifier = _notifier();
      final f1 = notifier.createJournal(date: '2026-01-01', geoMode: 'start_of_day');
      final f2 = notifier.createJournal(date: '2026-01-02', geoMode: 'start_of_day');

      expect(notifier.items.length, 2);
      final ids = notifier.items
          .map((i) => i['journal']?['id']?.toString())
          .toSet();
      expect(ids.length, 2,
          reason: 'placeholder ids must be unique per call, or the map '
              'marker layer would see duplicate ValueKeys');

      gate.complete();
      await Future.wait([f1, f2]);
      expect(notifier.items, isEmpty);
    });
  });

  test('a successful createMemory returns true and leaves the real item behind',
      () async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/memories/') {
            return http.Response('{}', 200);
          }
          if (req.method == 'GET' && req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                jsonEncode({
                  'name': 'Trip',
                  'items': [
                    {
                      'item_type': 'memory',
                      'memory': {
                        'id': 'mem-real-1',
                        'date': '2026-01-01',
                        'photos': <String>[],
                      },
                    },
                  ],
                }),
                200);
          }
          return http.Response('{}', 404);
        }));

    final notifier = _notifier();
    final ok = await notifier.createMemory(date: '2026-01-01', geoMode: 'start_of_day');

    expect(ok, isTrue);
    expect(notifier.items, hasLength(1));
    expect(notifier.items.single['memory']['id'], 'mem-real-1');
  });
}
