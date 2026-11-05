import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/photos/album_upgrade_service.dart';

AlbumUpgradeService _service(Future<http.Response> Function(http.Request) h) =>
    AlbumUpgradeService(client: ApiClient(httpClient: MockClient(h))..setToken('jwt'));

http.Response _json(int status, Object body) => http.Response(jsonEncode(body), status,
    headers: {'content-type': 'application/json'});

void main() {
  test('a 409 carries its code and message', () async {
    final s = _service((_) async => _json(409, {
          'detail': {'code': 'apply_in_progress', 'message': 'Photos are being replaced.'}
        }));
    await expectLater(
      s.apply(1),
      throwsA(isA<UpgradeFailure>()
          .having((e) => e.code, 'code', 'apply_in_progress')
          .having((e) => e.message, 'message', 'Photos are being replaced.')),
    );
  });

  test('a 503 is the no-worker case', () async {
    final s = _service((_) async => _json(503, {'detail': 'No worker is processing photo jobs'}));
    await expectLater(
      s.startScan(const ProjectRef(name: 'T'),
          albumId: 'a', includeMemories: true, includeJournal: false),
      throwsA(isA<UpgradeFailure>().having((e) => e.noWorker, 'noWorker', isTrue)),
    );
  });

  test('latest keeps the owner param and parses the summary', () async {
    late Uri seen;
    final s = _service((r) async {
      seen = r.url;
      return _json(200, {
        'scan': {
          'id': 3,
          'album_id': 'a',
          'status': 'ready',
          'apply_status': 'idle',
          'progress_done': 0,
          'progress_total': 0,
          'include_memories': false,
          'include_journal': true,
          'summary': {
            'counts': {'proposed': 2},
            'confidence': {'high': 1, 'low': 1},
            'selected': 1,
            'selected_bytes_delta': 50,
            'storage': {'used': 10, 'limit': 100},
          },
        },
        'newer': null,
        'photo_counts': {'memories': null, 'journal': 4},
      });
    });
    final l = await s.latest(const ProjectRef(name: 'My Trip', ownerId: 5));

    expect(seen.path, '/api/projects/My%20Trip/photo-upgrade/scans/latest');
    expect(seen.queryParameters['owner'], '5');
    expect(l.memoryPhotos, isNull);
    expect(l.journalPhotos, 4);
    expect(l.scan!.summary!.free, 90);
    expect(l.scan!.summary!.overStorage, isFalse);
  });

  test('assetThumbUrl asks for the preview only on request', () {
    final s = _service((_) async => _json(200, {}));
    expect(s.assetThumbUrl('x'), endsWith('/api/immich/assets/x/thumbnail'));
    expect(s.assetThumbUrl('x', preview: true), endsWith('/api/immich/assets/x/thumbnail?size=preview'));
  });

  test('unreadable_photos is read, and a missing value is 0', () async {
    Map<String, dynamic> scan(Map<String, dynamic> extra) => {
          'id': 3, 'album_id': 'a', 'status': 'ready', 'apply_status': 'idle',
          'progress_done': 0, 'progress_total': 0, 'include_memories': true,
          'include_journal': false, ...extra,
        };
    expect(UpgradeScan.fromJson(scan({'unreadable_photos': 4})).unreadablePhotos, 4);
    expect(UpgradeScan.fromJson(scan({})).unreadablePhotos, 0);
  });

  test('every error code has its own sentence', () {
    for (final code in [
      'too_large', 'unsupported_format', 'not_found_in_immich', 'immich_permission',
      'immich_unreachable', 'photo_gone', 'storage_full', 'invalid_image',
    ]) {
      expect(upgradeErrorSentence(code), isNot(upgradeErrorSentence('unknown')), reason: code);
    }
    expect(upgradeErrorSentence('immich_permission'), contains('asset.download'));
  });
}
