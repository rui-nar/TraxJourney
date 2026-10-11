/// Reading the server's message out of an error body (issue #429).
///
/// A regex stopping at the first quote cut messages short — an admin saw
/// `…follow \` — and choked on nothing but plain ASCII being safe.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/api/client.dart';

void main() {
  test('a detail with quotes and non-ASCII text comes out whole', () {
    const message = 'Follow "Deleting an account" — café, naïve, 日本語.';
    final body = jsonEncode({'detail': message, 'code': 'x'});
    expect(apiErrorDetail(body), message);
  });

  test('a body that is not JSON comes back as it is', () {
    const body = '<html>502 Bad Gateway</html>';
    expect(apiErrorDetail(body), body);
  });

  test('a detail that is not a message falls back to the body', () {
    // FastAPI validation errors carry a list.
    final body = jsonEncode({'detail': [{'msg': 'field required'}]});
    expect(apiErrorDetail(body), body);
  });

  group('apiErrorMessage (issue #480)', () {
    test('a detail with an escaped quote and non-ASCII text comes out whole',
        () {
      const message = 'Follow "Deleting an account" — café, naïve, 日本語.';
      final e = ApiException(409, jsonEncode({'detail': message}));
      expect(apiErrorMessage(e), message);
      expect(apiErrorReason(e), message);
    });

    test('a validation error list gives its first reason', () {
      final e = ApiException(422, jsonEncode({
        'detail': [
          {'loc': ['body', 'name'], 'msg': 'Value error, Too long', 'type': 'x'},
          {'loc': ['body', 'other'], 'msg': 'second', 'type': 'x'},
        ]
      }));
      expect(apiErrorMessage(e), 'Too long');
    });

    test('a body without a readable detail falls back to the exception text',
        () {
      final html = ApiException(502, '<html>Bad Gateway</html>');
      expect(apiErrorReason(html), isNull);
      expect(apiErrorMessage(html), html.toString());

      final structured = ApiException(409, jsonEncode({'detail': {'code': 'x'}}));
      expect(apiErrorReason(structured), isNull);
      expect(apiErrorMessage(structured), structured.toString());
    });

    test('a redirect keeps its server-URL hint', () {
      final e = ApiException(302, '', location: 'https://example.com/');
      expect(apiErrorMessage(e), contains('server redirected to'));
    });

    test('any other exception shows its own message', () {
      expect(apiErrorReason(Exception('No project open')), isNull);
      expect(apiErrorMessage(Exception('No project open')), 'No project open');
      expect(apiErrorMessage(StateError('bad')), 'Bad state: bad');
    });
  });
}
