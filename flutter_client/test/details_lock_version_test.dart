// A trip-details save never stores data older than the trip's version
// (issue #379, U10b).
//
// getDetails fetches the full ~12 MB payload. An edit made while that fetch is
// in flight bumps the trip's lock_version, and the edit's /meta records the new
// one; the response, from before the edit, used to be stored under it, where no
// later /meta could tell it was stale.

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

http.Response _json(Object body) => http.Response(jsonEncode(body), 200);

void main() {
  late int version;
  late Completer<void> releaseDetails;
  late Completer<void> detailsRequested;

  setUp(() {
    projectDataCache.resetForTest();
    version = 1;
    releaseDetails = Completer<void>();
    detailsRequested = Completer<void>();
    api = ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        final path = req.url.path;
        if (path == '/api/projects/Trip/meta') {
          return _json({'name': 'Trip', 'lock_version': version});
        }
        if (path == '/api/projects/Trip') {
          final v = version;
          detailsRequested.complete();
          await releaseDetails.future;
          return _json({'name': 'Trip', 'lock_version': v});
        }
        return _json(<String, dynamic>{});
      }),
    );
  });

  test('a details fetch in flight across an edit stores nothing', () async {
    final service = ProjectService();
    await service.getDetailsMeta(_ref);
    final fetch = service.getDetails(_ref);
    await detailsRequested.future;

    version = 2; // the edit
    await service.getDetailsMeta(_ref); // its /meta records version 2
    releaseDetails.complete();
    await fetch;

    expect(await projectDataCache.readFullDetails(_ref), isNull,
        reason: 'details from version 1 must not be stored for version 2');
  });

  test('a details fetch with an unchanged version still stores', () async {
    final service = ProjectService();
    await service.getDetailsMeta(_ref);
    final fetch = service.getDetails(_ref);
    await detailsRequested.future;
    releaseDetails.complete();
    await fetch;

    expect((await projectDataCache.readFullDetails(_ref))!['lock_version'], 1);
  });
}
