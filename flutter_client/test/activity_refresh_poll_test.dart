// Async Strava re-fetch of a single activity (issue #148).
//
// The re-fetch used to be one long POST. The server's two Strava calls can take
// minutes (rate-limiter waits, 429 backoff), so the request reliably exceeded
// ApiClient's 30 s timeout and the tile showed
// "Re-fetch failed: TimeoutException after 0:00:30.000000: Future not completed"
// for work the server had usually completed. The server now returns 202 with the
// activity marked `pending` and the client polls /meta for the verdict.
//
// Covers:
//  - a pending → resolved sequence reloads the project from the full endpoint
//    (not from /meta, which omits polylines);
//  - a failed verdict surfaces the server's message;
//  - a stale error from an earlier operation never reports a good re-fetch as a
//    failure (the toast reads `error` after awaiting);
//  - a trigger that fails outright reports without polling;
//  - transient poll errors are retried rather than treated as a failure;
//  - an activity deleted mid-refresh stops the poll quietly;
//  - navigating away cancels the poll;
//  - the poll gives up at its own deadline, which is what bounds a job orphaned
//    by a server restart.

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');
const _fast = Duration(milliseconds: 1);

Map<String, dynamic> _activity({
  int id = 111,
  String? refreshStatus,
  String? refreshError,
  String name = 'Ride',
  String? polyline,
}) => {
      'id': id,
      'name': name,
      'type': 'Ride',
      'refresh_status': refreshStatus,
      'refresh_error': refreshError,
      if (polyline != null) 'map': {'summary_polyline': polyline},
    };

Map<String, dynamic> _payload(List<Map<String, dynamic>> activities) => {
      'name': 'Trip',
      'activities': activities,
      'items': [
        for (final a in activities)
          {'item_type': 'activity', 'activity_id': a['id']},
      ],
    };

/// Serves a scripted sequence of /meta responses, then a full /details payload.
class _PollService extends ProjectService {
  _PollService({
    required this.metaSequence,
    this.detailsActivities,
    this.triggerError,
    this.metaThrowsOnCall = const <int>{},
    this.detailsError,
  });

  /// One entry per expected poll tick.
  final List<List<Map<String, dynamic>>> metaSequence;
  final List<Map<String, dynamic>>? detailsActivities;
  final Object? triggerError;
  final Set<int> metaThrowsOnCall;
  final Object? detailsError;

  int metaCalls = 0;
  int detailsCalls = 0;
  int geoCalls = 0;
  int triggerCalls = 0;

  @override
  Future<Map<String, dynamic>> refreshActivity(
      ProjectRef ref, int activityId) async {
    triggerCalls++;
    if (triggerError != null) throw triggerError!;
    return {'status': 'pending', 'refresh_status': 'pending'};
  }

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    final call = metaCalls++;
    if (metaThrowsOnCall.contains(call)) throw Exception('network blip');
    // Hold the last scripted state if polled more times than scripted.
    final idx = call < metaSequence.length ? call : metaSequence.length - 1;
    return _payload(metaSequence[idx]);
  }

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async {
    detailsCalls++;
    if (detailsError != null) throw detailsError!;
    return _payload(detailsActivities ?? const []);
  }

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async {
    geoCalls++;
    return {'type': 'FeatureCollection', 'features': <dynamic>[]};
  }
}

/// A notifier with [ref] set, bypassing load()'s network calls.
ProjectNotifier _notifier(ProjectService service) =>
    ProjectNotifier(service)..ref = _ref;

void main() {
  group('refreshActivity polling', () {
    test('pending then resolved reloads from the full details endpoint',
        () async {
      // /meta omits summary_polyline, so adopting the poll payload would blank
      // the very track the re-fetch just updated. The data must come from
      // getDetails.
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'pending')],
          [_activity(refreshStatus: 'pending')],
          [_activity(refreshStatus: 'resolved')],
        ],
        detailsActivities: [
          _activity(
              refreshStatus: 'resolved', name: 'Fresh name', polyline: 'abc'),
        ],
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, isNull);
      expect(service.triggerCalls, 1);
      expect(service.metaCalls, 3);
      expect(service.detailsCalls, 1, reason: 'one heavy fetch after the verdict');
      expect(n.activities.single['name'], 'Fresh name');
      expect(n.activities.single['map']['summary_polyline'], 'abc');
    });

    test('failed verdict surfaces the server error message', () async {
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'pending')],
          [
            _activity(
                refreshStatus: 'failed',
                refreshError: 'Strava fetch failed: 502 upstream'),
          ],
        ],
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, 'Strava fetch failed: 502 upstream');
      expect(service.detailsCalls, 0, reason: 'nothing to reload on failure');
    });

    test('a stale error does not report a successful re-fetch as failed',
        () async {
      // The toast decides success/failure by reading `error` after awaiting, so
      // a message left over from an unrelated operation used to report a good
      // re-fetch as a failure.
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'resolved')],
        ],
        detailsActivities: [_activity(refreshStatus: 'resolved')],
      );
      final n = _notifier(service)..error = 'some earlier failure';

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, isNull);
    });

    test('a trigger failure reports without polling', () async {
      final service = _PollService(
        metaSequence: const [],
        triggerError: Exception(
            '{"detail":"This activity has a locally edited track."}'),
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, 'This activity has a locally edited track.');
      expect(service.metaCalls, 0);
    });

    test('a transient poll error is retried, not reported as a failure',
        () async {
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'pending')],
          [_activity(refreshStatus: 'pending')], // this call throws
          [_activity(refreshStatus: 'resolved')],
        ],
        metaThrowsOnCall: {1},
        detailsActivities: [_activity(refreshStatus: 'resolved')],
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, isNull);
      expect(service.metaCalls, 3);
      expect(service.detailsCalls, 1);
    });

    test('an activity deleted mid-refresh stops the poll quietly', () async {
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'pending')],
          [_activity(id: 999)], // 111 is gone
        ],
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, isNull);
      expect(service.detailsCalls, 0);
    });

    test('navigating to another project cancels the poll', () async {
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'pending')],
          [_activity(refreshStatus: 'resolved')],
        ],
        detailsActivities: [_activity(refreshStatus: 'resolved')],
      );
      final n = _notifier(service);

      final future = n.refreshActivity(111,
          pollInterval: const Duration(milliseconds: 20),
          pollTimeout: const Duration(seconds: 5));
      n.ref = const ProjectRef(name: 'Other trip');
      await future;

      expect(service.detailsCalls, 0, reason: 'no reload for a stale project');
    });

    test('a details fetch failure after a good verdict is surfaced', () async {
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'resolved')],
        ],
        detailsError: Exception('{"detail":"Project not found"}'),
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, 'Project not found');
    });

    test(
        'a trigger timeout is a sentence, not "Future not completed" '
        '(issue #190)', () async {
      final service = _PollService(
        metaSequence: const [],
        triggerError:
            TimeoutException('after 0:00:30.000000', const Duration(seconds: 30)),
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, isNotNull);
      expect(n.error, isNot(contains('TimeoutException')));
      expect(n.error, isNot(contains('Future not completed')));
      expect(service.metaCalls, 0);
    });

    test(
        'a post-verdict details timeout is a sentence, not "Future not '
        'completed" (issue #190)', () async {
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'resolved')],
        ],
        detailsError:
            TimeoutException('after 0:00:30.000000', const Duration(seconds: 30)),
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(seconds: 5));

      expect(n.error, isNotNull);
      expect(n.error, isNot(contains('TimeoutException')));
      expect(n.error, isNot(contains('Future not completed')));
    });

    test('the poll gives up once its own deadline passes', () async {
      final service = _PollService(
        metaSequence: [
          [_activity(refreshStatus: 'pending')],
        ],
      );
      final n = _notifier(service);

      await n.refreshActivity(111,
          pollInterval: _fast, pollTimeout: const Duration(milliseconds: 30));

      expect(n.error, contains('taking longer than expected'));
    });
  });

}
