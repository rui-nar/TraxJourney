// Regression test for a load-performance audit finding: ProjectNotifier.load()
// awaited _loadSyncMeta()/_loadShareInfo() inside its try block, before the
// finally that clears isLoading — so the panel stayed non-interactive until
// those two small, map/activity-panel-irrelevant calls also returned, even
// though _loadFullGeoProgressively a few lines below is correctly fired
// unawaited. Fixed by backgrounding them the same way, with a new
// isSyncMetaLoaded flag a screen reading autoSyncEnabled/linkedPsTripId/
// shareToken/shareTokenNoMemories directly can gate on instead of assuming
// isLoading clearing means they're already populated.

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

http.Response _json(Map<String, dynamic> body) =>
    http.Response(jsonEncode(body), 200);

void main() {
  setUp(() => projectDataCache.resetForTest());

  test(
      'isLoading clears without waiting on the sync-meta/share-info fetch, '
      'and isSyncMetaLoaded flips true once it actually lands', () async {
    final syncMetaGate = Completer<void>();

    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          final path = req.url.path;
          if (path == '/api/projects/Trip/meta') {
            return _json({'name': 'Trip', 'activities': [], 'items': [],
              'people': [], 'groups': []});
          }
          if (path == '/api/geo/project/low-res' || path == '/api/geo/project') {
            return _json({'type': 'FeatureCollection', 'features': []});
          }
          if (path == '/api/projects/Trip/sync-meta') {
            // Held open until the test explicitly releases it, so load()'s
            // own completion can be observed strictly before this settles.
            await syncMetaGate.future;
            return _json({'auto_sync_enabled': false, 'linked_ps_trip_id': 7});
          }
          if (path == '/api/projects/Trip/share-info') {
            return _json({'share_token': 'tok-abc'});
          }
          return _json({});
        }));

    final notifier = ProjectNotifier(ProjectService());

    await notifier.load(_ref);

    expect(notifier.isLoading, isFalse,
        reason: 'the panel must become interactive without waiting on '
            'sync-meta/share-info');
    expect(notifier.isSyncMetaLoaded, isFalse,
        reason: 'the background sync-meta/share-info fetch has not landed '
            'yet — /sync-meta is still gated');
    // Defaults, not yet the mocked values below — proves nothing raced ahead.
    expect(notifier.autoSyncEnabled, isTrue);
    expect(notifier.shareToken, isNull);

    syncMetaGate.complete();
    // Let the now-unblocked background fetch (and its notifyListeners) land.
    for (var i = 0; i < 20 && !notifier.isSyncMetaLoaded; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }

    expect(notifier.isSyncMetaLoaded, isTrue);
    expect(notifier.autoSyncEnabled, isFalse);
    expect(notifier.linkedPsTripId, 7);
    expect(notifier.shareToken, 'tok-abc');
  });
}
