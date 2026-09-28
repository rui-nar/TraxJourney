// The social share sheet hands photo bytes to third parties, so they must be
// the stripped copies (location and device EXIF removed), fetched as the
// signed-in user through the app's own client — never the owner's originals,
// and never by creating a share link (issue #430).
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/share/share_asset_source_impl.dart';

const _base = 'http://trax.example.com';

void main() {
  late ApiClient savedApi;
  late List<http.Request> seen;

  setUp(() {
    savedApi = api;
    seen = [];
    api = ApiClient(
      baseUrl: _base,
      httpClient: MockClient((req) async {
        seen.add(req);
        if (req.url.path.endsWith('/u-missing/shareable')) {
          return http.Response('{"detail":"File not found"}', 404);
        }
        return http.Response.bytes([1, 2, 3], 200);
      }),
    )..setToken('jwt-for-owner');
  });

  tearDown(() => api = savedApi);

  ShareAssetSourceImpl source() => ShareAssetSourceImpl(
        ProjectNotifier(ProjectService()),
        () => throw StateError('no context needed'),
      );

  test('shareablePhotoPath is the authenticated stripped-copy route', () {
    expect(
      shareablePhotoPath(memoryId: 7, uuid: 'u1'),
      '/api/memories/7/photos/u1/shareable',
    );
  });

  test('photos come from the stripped-copy route, as the signed-in user',
      () async {
    final bytes = await source().fetchPhotos(7, ['u1', 'u2']);

    expect(bytes, hasLength(2));
    expect(seen.map((r) => r.url.toString()), [
      '$_base/api/memories/7/photos/u1/shareable',
      '$_base/api/memories/7/photos/u2/shareable',
    ]);
    for (final r in seen) {
      expect(r.method, 'GET');
      expect(r.headers['Authorization'], 'Bearer jwt-for-owner');
    }
  });

  test('sharing never touches a share link', () async {
    final notifier = ProjectNotifier(ProjectService());
    await ShareAssetSourceImpl(notifier, () => throw StateError('unused'))
        .fetchPhotos(7, ['u1']);

    expect(notifier.shareToken, isNull);
    expect(seen.map((r) => r.url.path), isNot(contains(endsWith('/share'))));
    expect(seen.map((r) => r.url.path), isNot(contains(contains('/api/share/'))));
  });

  test('a photo the server refuses is skipped, never fetched as the original',
      () async {
    final bytes = await source().fetchPhotos(7, ['u-missing', 'u2']);

    expect(bytes, hasLength(1));
    expect(seen.map((r) => r.url.path), [
      '/api/memories/7/photos/u-missing/shareable',
      '/api/memories/7/photos/u2/shareable',
    ]);
  });
}
