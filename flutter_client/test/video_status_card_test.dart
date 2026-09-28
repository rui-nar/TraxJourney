// Tests VideoStatusNotifier's poll loop and in-app download against a fake
// HTTP client, the same approach as poster_status_card_test.dart: a zero
// poll interval plus pumpEventQueue() instead of real waits.

import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/video_status_card.dart';

http.Response _json(int status, Object body) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('polls to done, then downloads the MP4 through the job route', () async {
    var polls = 0;
    final paths = <String>[];
    final client = ApiClient(httpClient: MockClient((req) async {
      paths.add(req.url.path);
      if (req.url.path.endsWith('/download')) {
        return http.Response.bytes([1, 2, 3], 200,
            headers: {'content-type': 'video/mp4'});
      }
      polls++;
      return _json(200, {
        'status': polls < 3 ? 'running' : 'done',
        'stage': 'Rendering',
        'progress': polls < 3 ? 0.5 : 1.0,
        'error_message': null,
        'expires_at': null,
      });
    }))
      ..setToken('jwt');
    Uint8List? saved;
    String? name;
    final n = VideoStatusNotifier(
      client: client,
      pollInterval: Duration.zero,
      save: (b, f) {
        saved = b;
        name = f;
      },
    );

    await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 4);
    expect(n.state, VideoCardState.rendering);
    await pumpEventQueue(times: 20);
    expect(n.state, VideoCardState.done);
    expect(polls, 3);
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('video_job_pending'), isNull);

    await n.download();
    expect(paths.last, '/api/projects/Trip/video/4/download');
    expect(saved, [1, 2, 3]);
    expect(name, 'Trip.mp4');
    expect(n.downloadError, isNull);
    n.dispose();
  });

  test('a failed job is shown as failed; expired as expired', () async {
    for (final (status, want) in [
      ('failed', VideoCardState.failed),
      ('expired', VideoCardState.expired),
    ]) {
      final client = ApiClient(httpClient: MockClient((_) async =>
          _json(200, {'status': status, 'progress': 0.0})));
      final n = VideoStatusNotifier(client: client, pollInterval: Duration.zero);
      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 1);
      await pumpEventQueue(times: 10);
      expect(n.state, want);
      n.dispose();
    }
  });

  test('resume picks a running job back up', () async {
    SharedPreferences.setMockInitialValues({
      'video_job_pending': jsonEncode({'name': 'Trip', 'jobId': 9}),
    });
    final client = ApiClient(httpClient: MockClient((_) async =>
        _json(200, {'status': 'running', 'progress': 0.25})));
    final n = VideoStatusNotifier(
        client: client, pollInterval: const Duration(hours: 1));
    await n.resume(const ProjectRef(name: 'Trip'));
    expect(n.state, VideoCardState.rendering);
    expect(n.progress, 0.25);
    n.dispose();
  });
}
