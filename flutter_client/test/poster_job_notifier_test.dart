// Tests createPosterJob's job-creation request against a fake HTTP client
// (mirrors polarsteps_token_expiry_test.dart's ApiClient(httpClient:
// MockClient(...)) injection pattern) — happy path (POST returns job_id) and
// an error path (ApiException surfaced without retrying/polling). Issue #14:
// the client no longer polls for job completion, so there is nothing beyond
// job creation left to test here.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/poster_consent_dialog.dart';
import 'package:traxjourney_client/src/projects/poster_job_notifier.dart';

http.Response _json(int status, Object body) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

void main() {
  group('createPosterJob', () {
    test('POSTs the request shape to the poster endpoint and returns the '
        'job id', () async {
      Map<String, dynamic>? capturedBody;
      final mock = MockClient((req) async {
        expect(req.method, 'POST');
        expect(req.url.path, '/api/projects/Trip/poster');
        capturedBody = jsonDecode(req.body) as Map<String, dynamic>;
        return _json(201, {'job_id': 42});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      final jobId = await createPosterJob(
        ref: const ProjectRef(name: 'Trip'),
        bounds: {'north': 1, 'south': 0, 'east': 1, 'west': 0},
        orientation: 'landscape',
        config: {'distance': true},
        memories: const [],
        client: client,
      );

      expect(jobId, 42);
      expect(capturedBody, {
        'bounds': {'north': 1, 'south': 0, 'east': 1, 'west': 0},
        'orientation': 'landscape',
        'paper_size': 'A0',
        'config': {'distance': true},
        'memories': [],
        'title_position': {'x': 0.0, 'y': 0.0},
        'title_text': null,
        'title_scale': 1.0,
      });
    });

    test('POSTs the given paper_size when one is provided', () async {
      Map<String, dynamic>? capturedBody;
      final mock = MockClient((req) async {
        capturedBody = jsonDecode(req.body) as Map<String, dynamic>;
        return _json(201, {'job_id': 42});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      await createPosterJob(
        ref: const ProjectRef(name: 'Trip'),
        bounds: {'north': 1, 'south': 0, 'east': 1, 'west': 0},
        orientation: 'landscape',
        config: {'distance': true},
        memories: const [],
        paperSize: 'A3',
        client: client,
      );

      expect(capturedBody?['paper_size'], 'A3');
    });

    test('POSTs the given title position/text/scale when provided', () async {
      Map<String, dynamic>? capturedBody;
      final mock = MockClient((req) async {
        capturedBody = jsonDecode(req.body) as Map<String, dynamic>;
        return _json(201, {'job_id': 42});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      await createPosterJob(
        ref: const ProjectRef(name: 'Trip'),
        bounds: {'north': 1, 'south': 0, 'east': 1, 'west': 0},
        orientation: 'landscape',
        config: {'distance': true},
        memories: const [],
        titlePosition: const {'x': 0.3, 'y': 0.6},
        titleText: 'Alps Only',
        titleScale: 1.5,
        client: client,
      );

      expect(capturedBody?['title_position'], {'x': 0.3, 'y': 0.6});
      expect(capturedBody?['title_text'], 'Alps Only');
      expect(capturedBody?['title_scale'], 1.5);
    });

    test('a job-creation API error is thrown as ApiException', () async {
      final mock = MockClient((req) async => http.Response('boom', 500));
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      expect(
        () => createPosterJob(
          ref: const ProjectRef(name: 'Trip'),
          bounds: const {},
          orientation: 'landscape',
          config: const {},
          memories: const [],
          client: client,
        ),
        throwsA(isA<ApiException>()),
      );
    });
  });

  group('fetchPosterPreview', () {
    test('POSTs the request shape to the preview endpoint and returns the '
        'raw PNG bytes', () async {
      final pngBytes = [0x89, 0x50, 0x4E, 0x47];
      Map<String, dynamic>? capturedBody;
      final mock = MockClient((req) async {
        expect(req.method, 'POST');
        expect(req.url.path, '/api/projects/Trip/poster/preview');
        capturedBody = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response.bytes(pngBytes, 200,
            headers: {'content-type': 'image/png'});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      final preview = await fetchPosterPreview(
        ref: const ProjectRef(name: 'Trip'),
        bounds: {'north': 1, 'south': 0, 'east': 1, 'west': 0},
        orientation: 'landscape',
        config: {'distance': true},
        memories: const [
          {'id': 1, 'lat': 0.5, 'lon': 0.5, 'date': '2024-01-01'}
        ],
        client: client,
      );

      expect(preview.bytes, pngBytes);
      expect(preview.warning, isNull);
      expect(preview.hasWarning, isFalse);
      expect(capturedBody, {
        'bounds': {'north': 1, 'south': 0, 'east': 1, 'west': 0},
        'orientation': 'landscape',
        'paper_size': 'A0',
        'config': {'distance': true},
        'memories': [
          {'id': 1, 'lat': 0.5, 'lon': 0.5, 'date': '2024-01-01'}
        ],
        'title_position': {'x': 0.0, 'y': 0.0},
        'title_text': null,
        'title_scale': 1.0,
      });
    });

    test('POSTs the given paper_size when one is provided', () async {
      Map<String, dynamic>? capturedBody;
      final mock = MockClient((req) async {
        capturedBody = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response.bytes([0x89, 0x50, 0x4E, 0x47], 200,
            headers: {'content-type': 'image/png'});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      await fetchPosterPreview(
        ref: const ProjectRef(name: 'Trip'),
        bounds: const {},
        orientation: 'portrait',
        config: const {},
        memories: const [],
        paperSize: 'A2',
        client: client,
      );

      expect(capturedBody?['paper_size'], 'A2');
    });

    test('POSTs the given title position/text/scale when provided', () async {
      Map<String, dynamic>? capturedBody;
      final mock = MockClient((req) async {
        capturedBody = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response.bytes([0x89, 0x50, 0x4E, 0x47], 200,
            headers: {'content-type': 'image/png'});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      await fetchPosterPreview(
        ref: const ProjectRef(name: 'Trip'),
        bounds: const {},
        orientation: 'landscape',
        config: const {},
        memories: const [],
        titlePosition: const {'x': 0.1, 'y': 0.2},
        titleText: 'Alps Only',
        titleScale: 0.75,
        client: client,
      );

      expect(capturedBody?['title_position'], {'x': 0.1, 'y': 0.2});
      expect(capturedBody?['title_text'], 'Alps Only');
      expect(capturedBody?['title_scale'], 0.75);
    });

    test('a non-2xx response throws ApiException rather than returning bytes',
        () async {
      final mock = MockClient((req) async => http.Response('boom', 500));
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      expect(
        () => fetchPosterPreview(
          ref: const ProjectRef(name: 'Trip'),
          bounds: const {},
          orientation: 'landscape',
          config: const {},
          memories: const [],
          client: client,
        ),
        throwsA(isA<ApiException>()),
      );
    });

    test('surfaces the X-Poster-Warning header when the basemap is missing',
        () async {
      // A degraded preview still returns 200 with a usable PNG; the warning
      // is what tells the user the grey background is a fault, not a design.
      final mock = MockClient((req) async => http.Response.bytes(
            [0x89, 0x50, 0x4E, 0x47],
            200,
            headers: {
              'content-type': 'image/png',
              'x-poster-warning': 'Map imagery unavailable: MAPBOX_TOKEN is not configured',
            },
          ));
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      final preview = await fetchPosterPreview(
        ref: const ProjectRef(name: 'Trip'),
        bounds: const {},
        orientation: 'landscape',
        config: const {},
        memories: const [],
        client: client,
      );

      expect(preview.hasWarning, isTrue);
      expect(preview.warning, contains('MAPBOX_TOKEN'));
      expect(preview.bytes, isNotEmpty, reason: 'preview still renders');
    });

    test('an empty warning header is not treated as a warning', () async {
      final mock = MockClient((req) async => http.Response.bytes(
            [0x89, 0x50, 0x4E, 0x47], 200,
            headers: {'content-type': 'image/png', 'x-poster-warning': ''},
          ));
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      final preview = await fetchPosterPreview(
        ref: const ProjectRef(name: 'Trip'),
        bounds: const {},
        orientation: 'landscape',
        config: const {},
        memories: const [],
        client: client,
      );

      expect(preview.hasWarning, isFalse);
    });
  });

  group('fetchPosterJobStatus', () {
    test('GETs the job status endpoint and returns the parsed fields',
        () async {
      final mock = MockClient((req) async {
        expect(req.method, 'GET');
        expect(req.url.path, '/api/projects/Trip/poster/42');
        return _json(200, {
          'status': 'running',
          'stage': 'Rendering basemap…',
          'error_message': null,
        });
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      final status = await fetchPosterJobStatus(
        ref: const ProjectRef(name: 'Trip'),
        jobId: 42,
        client: client,
      );

      expect(status.status, 'running');
      expect(status.stage, 'Rendering basemap…');
      expect(status.errorMessage, isNull);
      expect(status.isDone, isFalse);
      expect(status.isFailed, isFalse);
      expect(status.isTerminal, isFalse);
    });

    test('a done status reports isDone/isTerminal', () async {
      final mock = MockClient((req) async =>
          _json(200, {'status': 'done', 'stage': null, 'error_message': null}));
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      final status = await fetchPosterJobStatus(
        ref: const ProjectRef(name: 'Trip'),
        jobId: 1,
        client: client,
      );

      expect(status.isDone, isTrue);
      expect(status.isTerminal, isTrue);
    });

    test('a failed status carries the error message and reports '
        'isFailed/isTerminal', () async {
      final mock = MockClient((req) async => _json(200, {
            'status': 'failed',
            'stage': null,
            'error_message': 'internal: mapbox 500',
          }));
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      final status = await fetchPosterJobStatus(
        ref: const ProjectRef(name: 'Trip'),
        jobId: 1,
        client: client,
      );

      expect(status.isFailed, isTrue);
      expect(status.isTerminal, isTrue);
      expect(status.errorMessage, 'internal: mapbox 500');
    });

    test('a non-2xx response throws ApiException', () async {
      final mock = MockClient((req) async => http.Response('boom', 500));
      final client = ApiClient(httpClient: mock)..setToken('jwt');

      expect(
        () => fetchPosterJobStatus(
          ref: const ProjectRef(name: 'Trip'),
          jobId: 1,
          client: client,
        ),
        throwsA(isA<ApiException>()),
      );
    });
  });

  group('poster consent', () {
    final memories = <Map<String, dynamic>>[
      {
        'id': 7,
        'lat': 1.0,
        'lon': 2.0,
        'date': null,
        'name': 'Sunset',
        'description': 'Lovely',
        'photo_uuids': ['p1'],
      },
    ];
    final consent409 = _json(409, {
      'detail': {
        'code': 'consent_required',
        'message': 'This trip is encrypted.',
        'consent_required': [7],
      },
    });

    Future<int?> run(List<Map<String, dynamic>> bodies,
        List<http.Response> responses, PosterConsentChoice choice,
        {List<int>? asked}) {
      var i = 0;
      final mock = MockClient((req) async {
        bodies.add(jsonDecode(req.body) as Map<String, dynamic>);
        return responses[i++];
      });
      return createPosterJobWithConsent(
        ref: const ProjectRef(name: 'Trip'),
        bounds: {'north': 1, 'south': 0, 'east': 1, 'west': 0},
        orientation: 'landscape',
        config: {'distance': true},
        memories: memories,
        askConsent: (n) async {
          asked?.add(n);
          return choice;
        },
        client: ApiClient(httpClient: mock)..setToken('jwt'),
      );
    }

    test('PosterConsentRequired parses the 409 and ignores other errors', () {
      final ok = PosterConsentRequired.fromApiException(ApiException(
          409,
          jsonEncode({
            'detail': {
              'code': 'consent_required',
              'message': 'm',
              'consent_required': [7, 8],
            }
          })));
      expect(ok!.memoryIds, [7, 8]);
      expect(ok.message, 'm');
      expect(PosterConsentRequired.fromApiException(ApiException(409, 'x')),
          isNull);
      expect(
          PosterConsentRequired.fromApiException(ApiException(
              409, jsonEncode({'detail': 'Project is locked'}))),
          isNull);
      expect(
          PosterConsentRequired.fromApiException(ApiException(500, '{}')),
          isNull);
    });

    test('createPosterJob sends plaintext_consent only when true', () async {
      final bodies = <Map<String, dynamic>>[];
      final mock = MockClient((req) async {
        bodies.add(jsonDecode(req.body) as Map<String, dynamic>);
        return _json(201, {'job_id': 1});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      for (final consent in [false, true]) {
        await createPosterJob(
          ref: const ProjectRef(name: 'Trip'),
          bounds: {'north': 1, 'south': 0, 'east': 1, 'west': 0},
          orientation: 'landscape',
          config: {},
          memories: const [],
          plaintextConsent: consent,
          client: client,
        );
      }
      expect(bodies[0].containsKey('plaintext_consent'), isFalse);
      expect(bodies[1]['plaintext_consent'], isTrue);
    });

    test('409 asks; agree resends the same memories with consent', () async {
      final bodies = <Map<String, dynamic>>[];
      final asked = <int>[];
      final id = await run(bodies,
          [consent409, _json(201, {'job_id': 9})], PosterConsentChoice.sendText,
          asked: asked);
      expect(id, 9);
      expect(asked, [1]);
      expect(bodies, hasLength(2));
      expect(bodies[0].containsKey('plaintext_consent'), isFalse);
      expect(bodies[1]['plaintext_consent'], isTrue);
      expect(bodies[1]['memories'], memories);
    });

    test('decline to send text resends without name/description and without '
        'consent, keeping ids and photos', () async {
      final bodies = <Map<String, dynamic>>[];
      final id = await run(bodies, [consent409, _json(201, {'job_id': 9})],
          PosterConsentChoice.withoutText);
      expect(id, 9);
      expect(bodies[1].containsKey('plaintext_consent'), isFalse);
      final m = (bodies[1]['memories'] as List).single as Map;
      expect(m['id'], 7);
      expect(m['name'], isNull);
      expect(m['description'], isNull);
      expect(m['photo_uuids'], ['p1']);
    });

    test('cancel sends nothing more and returns null', () async {
      final bodies = <Map<String, dynamic>>[];
      final id = await run(bodies, [consent409], PosterConsentChoice.cancel);
      expect(id, isNull);
      expect(bodies, hasLength(1));
    });

    test('a plaintext trip never asks', () async {
      final bodies = <Map<String, dynamic>>[];
      final asked = <int>[];
      final id = await run(bodies, [_json(201, {'job_id': 3})],
          PosterConsentChoice.cancel,
          asked: asked);
      expect(id, 3);
      expect(asked, isEmpty);
      expect(bodies, hasLength(1));
    });

    test('a non-consent failure rethrows', () async {
      final bodies = <Map<String, dynamic>>[];
      expect(
          run(bodies, [http.Response('boom', 500)], PosterConsentChoice.cancel),
          throwsA(isA<ApiException>()));
    });
  });
}
