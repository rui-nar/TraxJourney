/// Importing a .traxj file larger than the server accepts (issue #434).
///
/// The server answers 413 with a plain "too large" detail, and that is what
/// the import screen must show: not an upgrade prompt, since no plan lifts
/// the limit. A 413 from a proxy in front of the server carries no JSON
/// detail, so it must still read as "too large", not as a raw response dump.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

Future<ProjectsNotifier> _uploadAnswered(http.Response response) async {
  final notifier = ProjectsNotifier(ProjectsService());
  await http.runWithClient(
    () => notifier.uploadProjectFile(bytes: [1, 2, 3], name: 'Trip'),
    () => MockClient((_) async => response),
  );
  return notifier;
}

void main() {
  test("the server's 413 detail is shown, with no upgrade prompt", () async {
    const detail = 'This file is too large to import. The limit is 50 MB.';
    final notifier = await _uploadAnswered(
        http.Response(jsonEncode({'detail': detail}), 413));

    expect(notifier.error, detail);
    expect(notifier.quotaError, isNull);
    expect(notifier.isLoading, isFalse);
  });

  test('a 413 without a JSON detail still reads as too large', () async {
    final notifier = await _uploadAnswered(http.Response(
        '<html><body>413 Request Entity Too Large</body></html>', 413));

    expect(notifier.error, 'This file is too large to import.');
    expect(notifier.quotaError, isNull);
  });
}
