// The social share sheet hands photo bytes to third parties, so they must
// come from the share-link route (the copy with location and device EXIF
// removed) and never from the authenticated owner route (issue #430).
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/share/share_asset_source_impl.dart';

const _base = 'http://trax.example.com';

void main() {
  late ApiClient savedApi;

  setUp(() {
    savedApi = api;
    api = ApiClient(baseUrl: _base);
  });

  tearDown(() => api = savedApi);

  ShareAssetSourceImpl source(ProjectNotifier n, List<http.Request> seen) =>
      ShareAssetSourceImpl(
        n,
        () => throw StateError('no context needed'),
        client: MockClient((req) async {
          seen.add(req);
          return http.Response.bytes([1, 2, 3], 200);
        }),
      );

  test('sharePhotoUrl is the share-link photo route', () {
    expect(
      sharePhotoUrl(base: _base, token: 'tok', memoryId: 7, uuid: 'u1'),
      '$_base/api/share/tok/photos/7/u1',
    );
  });

  test('photos are fetched through the share link, unauthenticated', () async {
    final n = ProjectNotifier(ProjectService())..shareToken = 'tok';
    final seen = <http.Request>[];

    final bytes = await source(n, seen).fetchPhotos(7, ['u1', 'u2']);

    expect(bytes, hasLength(2));
    expect(seen.map((r) => r.url.toString()), [
      '$_base/api/share/tok/photos/7/u1',
      '$_base/api/share/tok/photos/7/u2',
    ]);
    for (final r in seen) {
      expect(r.url.path, isNot(contains('/api/memories/')));
      expect(r.headers.keys.map((k) => k.toLowerCase()),
          isNot(contains('authorization')));
    }
  });

  test('a missing share token is created first, as the link resolver does',
      () async {
    api = ApiClient(
      baseUrl: _base,
      httpClient: MockClient((req) async {
        expect(req.method, 'POST');
        expect(req.url.path, endsWith('/share'));
        return http.Response(jsonEncode({'share_token': 'fresh'}), 200);
      }),
    );
    final n = ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip');
    final seen = <http.Request>[];

    await source(n, seen).fetchPhotos(7, ['u1']);

    expect(n.shareToken, 'fresh');
    expect(seen.single.url.toString(), '$_base/api/share/fresh/photos/7/u1');
  });

  test('with no token and no way to make one, no photo is fetched at all',
      () async {
    final n = ProjectNotifier(ProjectService()); // no project open
    final seen = <http.Request>[];

    final bytes = await source(n, seen).fetchPhotos(7, ['u1']);

    expect(bytes, isEmpty);
    expect(seen, isEmpty, reason: 'never the owner route as a fallback');
  });
}
