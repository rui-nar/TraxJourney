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
}
