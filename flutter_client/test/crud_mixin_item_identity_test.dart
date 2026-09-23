// Regression tests for a mutation-audit finding: several ProjectNotifier CRUD
// methods (memory/journal/encounter create/update/delete, reorderItems)
// mutated `items` in place (removeWhere/insert/removeAt/index-assignment)
// instead of reassigning a new list. map_panel.dart's marker/polyline cache
// and ProjectNotifier's own dayStats/orderedDayKeys caches all invalidate via
// identical(items, _last...), which a same-object mutation never trips — so
// a deleted memory's marker could linger on the map indefinitely, an edited
// journal entry's pin wouldn't move until an unrelated reload happened to
// land, etc. Fixed by building a new list (and, where an item's own map is
// edited, a new item map too) and reassigning `items` before notifying.
//
// This covers the pure local-mutation helpers directly (no network needed)
// and the synchronous-prefix identity change of the create/reorder flows
// (the property the fix guarantees, checked before their network await
// resolves) — the update/delete flows for journal/memory/encounter all
// follow the exact same mechanical pattern.

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

http.Response _json(Map<String, dynamic> body) =>
    http.Response(jsonEncode(body), 200);

Map<String, dynamic> _memoryItem(String id) => {
      'item_type': 'memory',
      'memory': {'id': id, 'date': '2024-06-01', 'photos': <String>[]},
    };

Map<String, dynamic> _journalItem(String id) => {
      'item_type': 'journal',
      'journal': {'id': id, 'date': '2024-06-01'},
    };

Map<String, dynamic> _encounterItem(String id) => {
      'item_type': 'encounter',
      'encounter': {'id': id, 'date': '2024-06-01'},
    };

void main() {
  setUp(() => projectDataCache.resetForTest());

  group('pure local-removal helpers (no network)', () {
    test('removeMemoryLocally reassigns items to a new list, content removed',
        () {
      final notifier = ProjectNotifier(ProjectService())..ref = _ref;
      final before = notifier.items = [_memoryItem('m1'), _memoryItem('m2')];

      notifier.removeMemoryLocally('m1');

      expect(notifier.items, isNot(same(before)),
          reason: 'a same-object mutation would never trip '
              'identical(items, _lastItems)-keyed caches');
      expect(notifier.items.length, 1);
      expect(notifier.items.first['memory']['id'], 'm2');
    });

    test('removeJournalLocally reassigns items to a new list, content removed',
        () {
      final notifier = ProjectNotifier(ProjectService())..ref = _ref;
      final before = notifier.items = [_journalItem('j1'), _journalItem('j2')];

      notifier.removeJournalLocally('j1');

      expect(notifier.items, isNot(same(before)));
      expect(notifier.items.length, 1);
      expect(notifier.items.first['journal']['id'], 'j2');
    });

    test(
        'removeEncounterLocally reassigns items to a new list, content '
        'removed — also the fix for activity_panel.dart\'s swipe-to-dismiss, '
        'which used to reach into notifier.items directly and never even '
        'called notifyListeners()', () {
      final notifier = ProjectNotifier(ProjectService())..ref = _ref;
      final before =
          notifier.items = [_encounterItem('e1'), _encounterItem('e2')];

      var notified = false;
      notifier.addListener(() => notified = true);
      notifier.removeEncounterLocally('e1');

      expect(notifier.items, isNot(same(before)));
      expect(notifier.items.length, 1);
      expect(notifier.items.first['encounter']['id'], 'e2');
      expect(notified, isTrue);
    });
  });

  group('create/reorder synchronous prefix', () {
    // createMemory/createJournal/createEncounter and reorderItems all do
    // their local list update synchronously, before their first `await` —
    // Dart guarantees the synchronous prefix of an async function body runs
    // immediately when called, so the identity change is observable before
    // the returned Future is even awaited.

    ApiClient mockedApi() => ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.url.path == '/api/projects/Trip/meta') {
            return _json({
              'name': 'Trip', 'activities': [], 'items': [],
              'people': [], 'groups': [],
            });
          }
          return _json({'id': 'server-1'});
        }));

    test('createMemory reassigns items before the network call resolves',
        () async {
      api = mockedApi();
      final notifier = ProjectNotifier(ProjectService())..ref = _ref;
      final before = notifier.items = [];

      final future = notifier.createMemory(date: '2024-06-01', geoMode: 'pin');
      expect(notifier.items, isNot(same(before)));
      expect(notifier.items, hasLength(1));

      await future;
    });

    test('reorderItems reassigns items before the network call resolves',
        () async {
      api = mockedApi();
      final notifier = ProjectNotifier(ProjectService())
        ..ref = _ref
        ..items = [_memoryItem('a'), _memoryItem('b'), _memoryItem('c')];
      final before = notifier.items;

      final future = notifier.reorderItems(0, 2);
      expect(notifier.items, isNot(same(before)));
      expect(notifier.items.map((i) => i['memory']['id']).toList(),
          ['b', 'c', 'a']);

      await future;
    });
  });
}
