/// Importing a trip ZIP (issue #469).
///
/// A `.zip` goes to `/import-zip` and a `.traxj` to `/import`, both keeping
/// the file's extension in the multipart filename. The server's 409, 402, 413,
/// 400 and 503 answers read the same for both file types.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

/// Uploads as [extension], the server answering the POST with [response] (or
/// a 201 when null); returns the notifier and the POST the server saw.
Future<(ProjectsNotifier, http.BaseRequest?)> _upload(
  String extension, {
  http.Response? response,
  ImportConflictChoice? onConflict,
}) async {
  final notifier = ProjectsNotifier(ProjectsService());
  http.BaseRequest? post;
  await http.runWithClient(
    () => notifier.uploadProjectFile(
        bytes: [1, 2, 3],
        name: 'Alps',
        extension: extension,
        onConflict: onConflict),
    () => MockClient((req) async {
      if (req.method != 'POST') return http.Response('[]', 200);
      post = req;
      return response ??
          http.Response(
              jsonEncode({'name': 'Alps', 'outcome': 'created'}), 201);
    }),
  );
  return (notifier, post);
}

http.Response _detail(int status, String detail) =>
    http.Response(jsonEncode({'detail': detail}), status);

void main() {
  group('routing', () {
    test('a ZIP goes to /import-zip and keeps its extension', () async {
      final (notifier, post) = await _upload('zip');

      expect(post!.url.path, '/api/projects/import-zip');
      expect((post as http.Request).body, contains('filename="Alps.zip"'));
      expect(notifier.error, isNull);
    });

    test('a .traxj goes to /import', () async {
      final (_, post) = await _upload('traxj');

      expect(post!.url.path, '/api/projects/import');
      expect((post as http.Request).body, contains('filename="Alps.traxj"'));
    });

    test('with no extension given, the upload is a .traxj', () async {
      final notifier = ProjectsNotifier(ProjectsService());
      http.BaseRequest? post;
      await http.runWithClient(
        () => notifier.uploadProjectFile(bytes: [1], name: 'Alps'),
        () => MockClient((req) async {
          if (req.method != 'POST') return http.Response('[]', 200);
          post = req;
          return http.Response(
              jsonEncode({'name': 'Alps', 'outcome': 'created'}), 201);
        }),
      );

      expect(post!.url.path, '/api/projects/import');
    });

    for (final (choice, sent) in [
      (ImportConflictChoice.keepBoth, 'copy'),
      (ImportConflictChoice.replace, 'replace'),
    ]) {
      test('$choice reaches /import-zip as on_conflict=$sent', () async {
        final (_, post) = await _upload('zip', onConflict: choice);

        expect(post!.url.path, '/api/projects/import-zip');
        expect(post.url.queryParameters['on_conflict'], sent);
      });
    }
  });

  group('a ZIP name conflict', () {
    test('is surfaced as a choice, not an error', () async {
      final (notifier, _) = await _upload(
        'zip',
        response: http.Response(
            jsonEncode({
              'detail': 'You already have a trip with this name.',
              'code': 'name_conflict',
              'name': 'Alps',
            }),
            409),
      );

      expect(notifier.nameConflict, 'Alps');
      expect(notifier.error, isNull);
      expect(notifier.isLoading, isFalse);
    });
  });

  group('error messages', () {
    for (final ext in ['zip', 'traxj']) {
      test("$ext: a 402 shows the server's storage message", () async {
        const detail = 'Not enough storage left on your plan.';
        final (notifier, _) =
            await _upload(ext, response: _detail(402, detail));

        expect(notifier.error, detail);
        expect(notifier.isLoading, isFalse);
      });

      test("$ext: a 400 shows the server's detail", () async {
        const detail = 'This archive could not be read.';
        final (notifier, _) =
            await _upload(ext, response: _detail(400, detail));

        expect(notifier.error, detail);
      });

      test("$ext: a 503 shows the server's detail", () async {
        const detail =
            'Another trip import is in progress. Try again in a minute.';
        final (notifier, _) =
            await _upload(ext, response: _detail(503, detail));

        expect(notifier.error, detail);
      });

      test('$ext: a 503 without a detail still says an import is running',
          () async {
        final (notifier, _) =
            await _upload(ext, response: http.Response('<html>503</html>', 503));

        expect(notifier.error,
            'Another trip import is in progress. Try again in a minute.');
      });

      test("$ext: a 413 shows the server's detail", () async {
        const detail = 'This file is too large to import. The limit is X.';
        final (notifier, _) =
            await _upload(ext, response: _detail(413, detail));

        expect(notifier.error, detail);
        expect(notifier.quotaError, isNull);
      });
    }

    test('a proxy 413 on a ZIP names the 1 GB cap', () async {
      final (notifier, _) =
          await _upload('zip', response: http.Response('<html>413</html>', 413));

      expect(notifier.error,
          'This file is too large to import. The limit is 1 GB.');
    });

    test('a proxy 413 on a .traxj names the 50 MB cap', () async {
      final (notifier, _) = await _upload('traxj',
          response: http.Response('<html>413</html>', 413));

      expect(notifier.error,
          'This file is too large to import. The limit is 50 MB.');
    });
  });
}
