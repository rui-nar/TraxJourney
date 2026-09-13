// A partial-batch import failure used to be fully unattributed: _importStep's
// catch-all discarded the exception and the photo-upload .catchError((_){})
// silently dropped upload failures, so the user only ever saw an aggregate
// "$added memories added" with no way to tell which steps failed or that
// some photos never made it. importSelected now tracks per-step outcome and
// per-step photo counts so a real summary can be shown.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/polarsteps_import_notifier.dart';

const _ref = ProjectRef(name: 'Trip');

http.Response _json(int status, Object body) =>
    http.Response(jsonEncode(body), status,
        headers: {'content-type': 'application/json'});

void main() {
  test('a batch with some failing steps reports both success and failure counts',
      () async {
    var nextMemId = 0;
    final mock = MockClient((req) async {
      if (req.method == 'POST' && req.url.path == '/api/memories/') {
        final body = jsonDecode(req.body) as Map<String, dynamic>;
        if (body['polarsteps_step_id'] == 2) {
          return _json(400, {'detail': 'Bad memory data'});
        }
        nextMemId++;
        return _json(201, {'id': nextMemId});
      }
      if (req.method == 'POST' && req.url.path.contains('/photos/from-url')) {
        return _json(202, {});
      }
      return _json(404, {'detail': 'unexpected ${req.url.path}'});
    });
    final client = ApiClient(httpClient: mock)..setToken('jwt');
    final notifier = PolarstepsImportNotifier(client: client)
      ..steps = [
        {'id': 1, 'name': 'Step 1', 'date': '2024-01-01', 'photos': []},
        {'id': 2, 'name': 'Step 2', 'date': '2024-01-02', 'photos': []},
        {'id': 3, 'name': 'Step 3', 'date': '2024-01-03', 'photos': []},
      ]
      ..selectedStepIds.addAll([1, 2, 3]);

    final created = await notifier.importSelected(_ref);

    // Not just an aggregate success count — the failure is attributable.
    expect(created, 2);
    expect(notifier.failedSteps, hasLength(1));
    expect(notifier.failedSteps.single.stepName, 'Step 2');
    expect(notifier.failedSteps.single.reason, contains('Bad memory data'));
  });

  test('a memory created with a failed photo upload is flagged as missing photos',
      () async {
    final mock = MockClient((req) async {
      if (req.method == 'POST' && req.url.path == '/api/memories/') {
        return _json(201, {'id': 1});
      }
      if (req.method == 'POST' && req.url.path.contains('/photos/from-url')) {
        final body = jsonDecode(req.body) as Map<String, dynamic>;
        // Second photo (order: 1) fails to upload; first succeeds.
        if (body['order'] == 1) return _json(500, {'detail': 'boom'});
        return _json(202, {});
      }
      return _json(404, {'detail': 'unexpected ${req.url.path}'});
    });
    final client = ApiClient(httpClient: mock)..setToken('jwt');
    final notifier = PolarstepsImportNotifier(client: client)
      ..steps = [
        {
          'id': 1,
          'name': 'Step 1',
          'date': '2024-01-01',
          'photos': [
            {'url': 'https://example.com/a.jpg'},
            {'url': 'https://example.com/b.jpg'},
          ],
        },
      ]
      ..selectedStepIds.add(1);

    final created = await notifier.importSelected(_ref);

    // The memory itself succeeded — it's not counted as a failed step —
    // but the missing photo is surfaced rather than silently dropped.
    expect(created, 1);
    expect(notifier.failedSteps, isEmpty);
    expect(notifier.stepsWithMissingPhotos, 1);
  });

  test('a fully successful batch has no failures and no missing photos', () async {
    final mock = MockClient((req) async {
      if (req.method == 'POST' && req.url.path == '/api/memories/') {
        return _json(201, {'id': 1});
      }
      if (req.method == 'POST' && req.url.path.contains('/photos/from-url')) {
        return _json(202, {});
      }
      return _json(404, {'detail': 'unexpected ${req.url.path}'});
    });
    final client = ApiClient(httpClient: mock)..setToken('jwt');
    final notifier = PolarstepsImportNotifier(client: client)
      ..steps = [
        {'id': 1, 'name': 'Step 1', 'date': '2024-01-01', 'photos': []},
      ]
      ..selectedStepIds.add(1);

    final created = await notifier.importSelected(_ref);

    expect(created, 1);
    expect(notifier.failedSteps, isEmpty);
    expect(notifier.stepsWithMissingPhotos, 0);
  });
}
