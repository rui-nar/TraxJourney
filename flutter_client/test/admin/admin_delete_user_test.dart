/// An admin deleting a user sees the server's reason when it is refused —
/// e.g. the user's paid plan could not be cancelled (issue #429) — not the raw
/// JSON body.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/admin/admin_service.dart';
import 'package:traxjourney_client/src/api/client.dart';

const _refusal = 'The paid plan on this account could not be cancelled, so the '
    'account was not deleted and nothing was removed. Please try again in a '
    'few minutes.';

void main() {
  test('a refused deletion carries the server message, not the body', () async {
    api = ApiClient(httpClient: MockClient((req) async {
      expect(req.method, 'DELETE');
      expect(req.url.path, '/api/admin/users/7');
      return http.Response(
        jsonEncode({
          'detail': _refusal,
          'code': 'subscription_cancel_failed',
          'request_id': 'r1',
        }),
        502,
        headers: {'content-type': 'application/json'},
      );
    }));

    await expectLater(
      AdminService().deleteUser(7),
      throwsA(isA<Exception>().having(
        (e) => e.toString().replaceFirst('Exception: ', ''),
        'message',
        _refusal,
      )),
    );
  });

  test('a message with quotes and non-ASCII text is shown whole', () async {
    const message = 'Nothing was deleted. Follow "Deleting an account" — '
        'naïve café.';
    api = ApiClient(httpClient: MockClient((req) async => http.Response(
          jsonEncode({'detail': message, 'code': 'billing_unavailable'}),
          409,
          headers: {'content-type': 'application/json; charset=utf-8'},
        )));

    await expectLater(
      AdminService().deleteUser(7),
      throwsA(isA<Exception>().having(
        (e) => e.toString().replaceFirst('Exception: ', ''),
        'message',
        message,
      )),
    );
  });

  test('a successful deletion does not throw', () async {
    api = ApiClient(httpClient: MockClient(
        (req) async => http.Response(jsonEncode({'ok': true}), 200,
            headers: {'content-type': 'application/json'})));

    await AdminService().deleteUser(7);
  });
}
