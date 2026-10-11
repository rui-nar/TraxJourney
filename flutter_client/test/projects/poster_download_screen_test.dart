/// The poster download page's error text (issue #480).
///
/// A regex stopping at the first escaped quote cut the server's message short.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/projects/poster_download_screen.dart';

// A detail with a quote and non-ASCII text.
const _detail = 'Can\'t use "Mon voyage" — café 日本';

ApiClient _client(http.Response Function(http.Request) handler) =>
    ApiClient(httpClient: MockClient((req) async => handler(req)));

http.Response _json(int status, Object body) => http.Response.bytes(
    utf8.encode(jsonEncode(body)), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

Future<void> _pump(WidgetTester tester, ApiClient client) async {
  await tester.pumpWidget(MaterialApp(
      home: PosterDownloadScreen(token: 'tok', apiClient: client)));
  // pump, not pumpAndSettle: a failed status leaves the spinner running.
  await tester.pump();
  await tester.pump();
}

void main() {
  // Issue #480.
  testWidgets('a status error with quotes and non-ASCII text is shown whole',
      (tester) async {
    await _pump(tester, _client((_) => _json(400, {'detail': _detail})));

    expect(find.text(_detail), findsOneWidget);
  });

  // Issue #480.
  testWidgets('a download error is shown whole', (tester) async {
    await _pump(tester, _client((req) {
      if (req.url.path.endsWith('/download')) {
        return _json(400, {'detail': _detail});
      }
      return _json(200, {'status': 'done'});
    }));

    await tester.tap(find.text('Download PNG'));
    await tester.pumpAndSettle();

    expect(find.text(_detail), findsOneWidget);
  });
}
