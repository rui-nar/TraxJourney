/// Importing a file that is not a readable trip (issue #451).
///
/// The server answers 400 with a sentence meant for the user, naming what is
/// wrong with the file. The import screen must show it as-is, apostrophe and
/// all, and must not treat it as a plan limit.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

void main() {
  test("the server's 400 detail is shown verbatim", () async {
    const detail = "This file isn't a valid TraxJourney trip: "
        'it is not valid JSON (line 1, column 32).';
    final notifier = ProjectsNotifier(ProjectsService());

    final saved = await http.runWithClient(
      () => notifier.uploadProjectFile(bytes: [1, 2, 3], name: 'Trip'),
      () => MockClient((_) async => http.Response(
            jsonEncode({'detail': detail, 'request_id': 'abcd1234'}),
            400,
            headers: {'content-type': 'application/json'},
          )),
    );

    expect(saved, isNull);
    expect(notifier.error, detail);
    expect(notifier.quotaError, isNull);
    expect(notifier.isLoading, isFalse);
  });
}
