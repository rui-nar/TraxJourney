// Tests PosterStatusNotifier's poll/give-up state machine against a fake
// HTTP client (mirrors poster_job_notifier_test.dart's ApiClient(httpClient:
// MockClient(...)) injection pattern) and a mocked shared_preferences
// backend (mirrors project_stats_screen_test.dart's
// SharedPreferences.setMockInitialValues({}) setup). Deliberately exercises
// the notifier alone, not PosterStatusCard — no widget tree is pumped, the
// same "test plain classes/functions" approach poster_job_notifier_test.dart
// and its now-deleted PosterJobNotifier predecessor used (see git history:
// `pollInterval: Duration.zero` there mirrors the same trick used below).
//
// Since polling here is driven by a real (not fake_async) Timer, tests use
// `pollInterval: Duration.zero` plus `pumpEventQueue()` to let the poll
// chain run to completion instead of waiting real wall-clock seconds. The
// 20-minute give-up window is exercised via the injectable `now` clock
// instead of actually waiting 20 minutes.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/poster_status_card.dart';

const _kPrefKey = 'poster_job_pending';

http.Response _json(int status, Object body) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('PosterStatusNotifier.start', () {
    test('starts hidden before start() is called', () {
      final n = PosterStatusNotifier();
      expect(n.state, PosterCardState.hidden);
    });

    test('switches to generating immediately and persists the job, without '
        'polling before pollInterval elapses', () async {
      var getCalls = 0;
      final mock = MockClient((req) async {
        getCalls++;
        return _json(200, {'status': 'running', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(
          client: client, pollInterval: const Duration(seconds: 5));

      final started = n.start(ref: const ProjectRef(name: 'Trip'), jobId: 42);
      // State flips synchronously — before the persistence write's await gap.
      expect(n.state, PosterCardState.generating);
      await started;

      expect(getCalls, 0, reason: 'first poll is not due for 5s');
      final prefs = await SharedPreferences.getInstance();
      final saved = jsonDecode(prefs.getString(_kPrefKey)!) as Map<String, dynamic>;
      expect(saved['name'], 'Trip');
      expect(saved['jobId'], 42);
    });

    test('replaces a previously tracked job — only the newest job is polled',
        () async {
      final polledIds = <String>[];
      final mock = MockClient((req) async {
        polledIds.add(req.url.pathSegments.last);
        return _json(200, {'status': 'running', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client, pollInterval: Duration.zero);

      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 1);
      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 2);
      await pumpEventQueue(times: 10);

      expect(polledIds, isNotEmpty);
      expect(polledIds.toSet(), {'2'}, reason: 'job 1s timer was cancelled by the second start()');
    });
  });

  group('PosterStatusNotifier polling to a terminal state', () {
    test('polls running -> done, then stops polling and clears the '
        'persisted job', () async {
      var pollCount = 0;
      final mock = MockClient((req) async {
        pollCount++;
        if (pollCount == 1) {
          return _json(200,
              {'status': 'running', 'stage': 'Rendering basemap…', 'error_message': null});
        }
        return _json(200, {'status': 'done', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client, pollInterval: Duration.zero);

      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 1);
      await pumpEventQueue(times: 30);

      expect(n.state, PosterCardState.done);
      expect(pollCount, 2);

      final callsAtDone = pollCount;
      await pumpEventQueue(times: 20);
      expect(pollCount, callsAtDone, reason: 'no more polling once terminal');

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_kPrefKey), isNull);
    });

    test('a failed status stops polling, shows failed, and clears the '
        'persisted job', () async {
      var pollCount = 0;
      final mock = MockClient((req) async {
        pollCount++;
        return _json(200,
            {'status': 'failed', 'stage': null, 'error_message': 'internal: mapbox 500'});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client, pollInterval: Duration.zero);

      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 2);
      await pumpEventQueue(times: 30);

      expect(n.state, PosterCardState.failed);
      final callsAtFailed = pollCount;
      await pumpEventQueue(times: 20);
      expect(pollCount, callsAtFailed, reason: 'no more polling once terminal');

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_kPrefKey), isNull);
    });

    test('a transient HTTP error keeps polling instead of surfacing as '
        'failed', () async {
      var pollCount = 0;
      final mock = MockClient((req) async {
        pollCount++;
        if (pollCount == 1) return http.Response('boom', 500);
        return _json(200, {'status': 'done', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client, pollInterval: Duration.zero);

      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 3);
      await pumpEventQueue(times: 30);

      expect(n.state, PosterCardState.done);
      expect(pollCount, greaterThanOrEqualTo(2));
    });
  });

  group('PosterStatusNotifier.dismiss', () {
    test('hides the card, clears the persisted job, and stops polling',
        () async {
      var getCalls = 0;
      final mock = MockClient((req) async {
        getCalls++;
        return _json(200, {'status': 'running', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client, pollInterval: Duration.zero);

      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 4);
      await pumpEventQueue(times: 5);
      final callsBeforeDismiss = getCalls;
      expect(callsBeforeDismiss, greaterThan(0));

      await n.dismiss();
      expect(n.state, PosterCardState.hidden);

      await pumpEventQueue(times: 20);
      expect(getCalls, callsBeforeDismiss, reason: 'no more polling after dismiss');

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_kPrefKey), isNull);
    });
  });

  group('PosterStatusNotifier give-up (rule: never a false failure by '
      'timeout)', () {
    test('stops polling silently after giveUpAfter and leaves the card '
        '"generating" rather than "failed"', () async {
      var fakeNow = DateTime(2026, 1, 1);
      var getCalls = 0;
      final mock = MockClient((req) async {
        getCalls++;
        return _json(200, {'status': 'running', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(
        client: client,
        pollInterval: Duration.zero,
        giveUpAfter: const Duration(minutes: 20),
        now: () => fakeNow,
      );

      await n.start(ref: const ProjectRef(name: 'Trip'), jobId: 5);
      await pumpEventQueue(times: 5);
      expect(getCalls, greaterThan(0), reason: 'polling was actually active');
      expect(n.state, PosterCardState.generating);

      // Jump the fake clock past the give-up threshold.
      fakeNow = fakeNow.add(const Duration(minutes: 21));
      final callsBeforeGiveUp = getCalls;
      await pumpEventQueue(times: 20);

      expect(getCalls, callsBeforeGiveUp,
          reason: 'give-up stops polling instead of one more failing check');
      expect(n.state, PosterCardState.generating,
          reason: 'a long-running job must never be shown as failed by timeout');
    });
  });

  group('PosterStatusNotifier.resume', () {
    test('does nothing when no job is persisted', () async {
      var getCalls = 0;
      final mock = MockClient((req) async {
        getCalls++;
        return _json(200, {'status': 'done', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client);

      await n.resume(const ProjectRef(name: 'Trip'));

      expect(n.state, PosterCardState.hidden);
      expect(getCalls, 0);
    });

    test('a terminal persisted job is checked once, then cleared without '
        'showing the card', () async {
      SharedPreferences.setMockInitialValues(
          {_kPrefKey: jsonEncode({'name': 'Trip', 'jobId': 6})});
      var getCalls = 0;
      final mock = MockClient((req) async {
        getCalls++;
        return _json(200, {'status': 'done', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client);

      await n.resume(const ProjectRef(name: 'Trip'));

      expect(n.state, PosterCardState.hidden);
      expect(getCalls, 1);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_kPrefKey), isNull);
    });

    test('a still-pending persisted job resumes polling and shows the card',
        () async {
      SharedPreferences.setMockInitialValues(
          {_kPrefKey: jsonEncode({'name': 'Trip', 'jobId': 7})});
      var pollCount = 0;
      final mock = MockClient((req) async {
        pollCount++;
        if (pollCount == 1) {
          return _json(200,
              {'status': 'running', 'stage': 'Placing pins', 'error_message': null});
        }
        return _json(200, {'status': 'done', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client, pollInterval: Duration.zero);

      await n.resume(const ProjectRef(name: 'Trip'));
      expect(n.state, PosterCardState.generating);
      expect(n.stage, 'Placing pins');

      await pumpEventQueue(times: 30);
      expect(n.state, PosterCardState.done);
      expect(pollCount, 2);
    });

    test('a persisted job for a different project is left untouched',
        () async {
      SharedPreferences.setMockInitialValues(
          {_kPrefKey: jsonEncode({'name': 'OtherTrip', 'jobId': 8})});
      var getCalls = 0;
      final mock = MockClient((req) async {
        getCalls++;
        return _json(200, {'status': 'done', 'stage': null, 'error_message': null});
      });
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client);

      await n.resume(const ProjectRef(name: 'Trip'));

      expect(n.state, PosterCardState.hidden);
      expect(getCalls, 0);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_kPrefKey), isNotNull);
    });

    test('an unreadable (stale) persisted job is cleared without showing '
        'the card', () async {
      SharedPreferences.setMockInitialValues(
          {_kPrefKey: jsonEncode({'name': 'Trip', 'jobId': 9})});
      final mock = MockClient((req) async => http.Response('gone', 404));
      final client = ApiClient(httpClient: mock)..setToken('jwt');
      final n = PosterStatusNotifier(client: client);

      await n.resume(const ProjectRef(name: 'Trip'));

      expect(n.state, PosterCardState.hidden);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_kPrefKey), isNull);
    });
  });
}
