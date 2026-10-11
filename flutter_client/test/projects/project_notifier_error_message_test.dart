/// The server's message in ProjectNotifier's errors (issue #480).
///
/// A regex stopping at the first escaped quote cut messages short, and the
/// share and export calls rethrew the raw JSON body instead of the message.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

// A detail with a quote and non-ASCII text.
const _detail = 'Can\'t use "Mon voyage" — café 日本';

http.Response _error(int status) => http.Response.bytes(
    utf8.encode(jsonEncode({'detail': _detail})), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

void main() {
  late ProjectNotifier notifier;

  setUp(() {
    api = ApiClient(httpClient: MockClient((req) async => _error(400)));
    notifier = ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip');
  });

  tearDown(() {
    notifier.dispose();
    api = ApiClient();
  });

  // Issue #480.
  test('errorMessage gives a quoted, non-ASCII server detail whole', () {
    expect(notifier.errorMessage(ApiException(400, jsonEncode({'detail': _detail}))),
        _detail);
  });

  // Issue #480.
  test('errorMessage gives a validation error its reason', () {
    final e = ApiException(422, jsonEncode({
      'detail': [
        {'msg': 'Value error, Name is too long'}
      ]
    }));
    expect(notifier.errorMessage(e), 'Name is too long');
  });

  // Issue #480.
  test('fetchExportBytes rethrows the bare detail, not the JSON body',
      () async {
    await expectLater(
      notifier.fetchExportBytes('/api/projects/Trip/export'),
      throwsA(isA<Exception>().having(
          (e) => e.toString(), 'message', 'Exception: $_detail')),
    );
  });

  // Issue #480.
  test('createShareToken rethrows the bare detail, not the JSON body',
      () async {
    await expectLater(
      notifier.createShareToken(),
      throwsA(isA<Exception>().having(
          (e) => e.toString(), 'message', 'Exception: $_detail')),
    );
  });
}
