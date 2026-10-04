// Segment route-resolve polling (issues #213, #278).
//
// `pollSegmentResolution` is the only code path that syncs a segment's
// `route_status` back into client state after a resolve is triggered.
//
// It used to be one loop per segment with a 2-minute deadline, after which it
// set an error and never polled again (#213). The server runs two resolve jobs
// at a time, so a third resolve queued behind them easily missed that window,
// and its route only reached the map once the trip was reopened (#278). Now one
// poller per trip covers every pending segment, with no deadline, backing off
// from 3 s to 15 s after 2 minutes and to 60 s after 10.
//
// Runs under testWidgets for its fake clock: a resolve that lands after five
// minutes is five minutes of polling.

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

Map<String, dynamic> _segmentItem(Map<String, dynamic> segment) =>
    {'item_type': 'segment', 'segment': segment};

Map<String, dynamic> _meta(List<Map<String, dynamic>> segments) =>
    {'items': [for (final s in segments) _segmentItem(s)]};

/// Serves a scripted sequence of /meta responses, one entry per poll tick.
class _PollService extends ProjectService {
  _PollService(this.metaSequence);

  final List<List<Map<String, dynamic>>> metaSequence;
  int metaCalls = 0;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    final call = metaCalls++;
    final idx = call < metaSequence.length ? call : metaSequence.length - 1;
    return _meta(metaSequence[idx]);
  }
}

/// A server holding each segment's current state, which a test changes as
/// resolve jobs finish. /meta answers with the state at the moment it is
/// asked; [gate], when set, holds the answer back.
class _Server extends ProjectService {
  final Map<String, Map<String, dynamic>> segments = {};
  final List<ProjectRef> metaRefs = [];
  Completer<void>? gate;

  int get metaCalls => metaRefs.length;

  void resolve(String id, {String polyline = '[[0,0],[1,1],[2,2]]'}) {
    segments[id] = {
      ...segments[id]!,
      'route_status': 'resolved',
      'route_mode': 'rail',
      'route_polyline': polyline,
    };
  }

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    metaRefs.add(ref);
    final snapshot = _meta([
      for (final s in segments.values) Map<String, dynamic>.from(s),
    ]);
    final g = gate;
    if (g != null) await g.future;
    return snapshot;
  }

  @override
  Future<Map<String, dynamic>> resolveTrainRoute(
    ProjectRef ref,
    String segId, {
    String? hafasProvider,
    String? trainNumber,
    String? date,
    bool force = false,
  }) async {
    segments[segId] = {...segments[segId]!, 'route_status': 'pending'};
    return {'status': 'pending', 'route_status': 'pending'};
  }
}

ProjectNotifier _notifier(ProjectService service, {String segId = 'seg-1'}) {
  final n = ProjectNotifier(service)..ref = _ref;
  n.items = [
    _segmentItem({'id': segId, 'route_status': 'pending'}),
  ];
  return n;
}

/// A notifier over [server], with every server segment in its items and an
/// empty map loaded.
ProjectNotifier _notifierOn(_Server server) {
  final n = ProjectNotifier(server)..ref = _ref;
  n.items = [
    for (final s in server.segments.values)
      _segmentItem(Map<String, dynamic>.from(s)),
  ];
  n.geo = {'type': 'FeatureCollection', 'features': <dynamic>[]};
  return n;
}

Map<String, dynamic> _seg(String id, {String status = 'idle'}) => {
      'id': id,
      'segment_type': 'train',
      'route_status': status,
      'route_mode': 'great_circle',
    };

/// The route mode the map draws for [segId], or null when it has no line.
String? _drawnMode(ProjectNotifier n, String segId) {
  for (final f in (n.geo?['features'] as List? ?? const [])) {
    final props = (f as Map)['properties'] as Map;
    if (props['segment_id'] == segId) return props['route_mode'] as String?;
  }
  return null;
}

String? _itemStatus(ProjectNotifier n, String segId) => n.items
    .map((i) => i['segment'] as Map)
    .firstWhere((s) => s['id'] == segId)['route_status'] as String?;

/// Starts waiting on [segId]; the outcome lands in the returned record.
({Map<String, dynamic>? result, Object? error}) Function() _await(
    Future<Map<String, dynamic>> future) {
  Map<String, dynamic>? result;
  Object? error;
  future.then<void>((r) {
    result = r;
  }, onError: (Object e) {
    error = e;
  });
  return () => (result: result, error: error);
}

void main() {
  group('pollSegmentResolution', () {
    testWidgets('a resolve still pending past the old 2-minute deadline sets '
        'no error and lands when it finishes (issue #278)', (tester) async {
      // Replaces the #213 test that expected an error at the deadline: that
      // deadline is removed by decision (docs/CLIENT_STATE_MAP_PLAN.md,
      // Decision 11), because it only moved the cliff.
      final server = _Server()..segments['seg-1'] = _seg('seg-1', status: 'pending');
      final n = _notifierOn(server);

      final outcome = _await(n.pollSegmentResolution('seg-1'));
      await tester.pump(const Duration(minutes: 3));
      expect(n.error, isNull);
      expect(outcome().result, isNull, reason: 'still waiting, not given up');

      server.resolve('seg-1');
      await tester.pump(const Duration(seconds: 15));
      expect(outcome().result?['route_status'], 'resolved');
      expect(_drawnMode(n, 'seg-1'), 'rail');
      expect(n.error, isNull);
      n.dispose();
    });

    testWidgets('resolving before the deadline clears any risk of a stale error',
        (tester) async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'resolved', 'route_degraded': false}
        ],
      ]);
      final n = _notifier(service);

      final outcome = _await(n.pollSegmentResolution('seg-1'));
      await tester.pump(const Duration(seconds: 5));
      final result = outcome().result!;

      expect(result['route_status'], 'resolved');
      expect(result['degraded'], false);
      expect(n.error, isNull);
      n.dispose();
    });

    testWidgets('a degraded resolution is reported back as resolved, not failed',
        (tester) async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'resolved', 'route_degraded': true}
        ],
      ]);
      final n = _notifier(service);

      final outcome = _await(n.pollSegmentResolution('seg-1'));
      await tester.pump(const Duration(seconds: 5));
      final result = outcome().result!;

      expect(result['route_status'], 'resolved');
      expect(result['degraded'], true);
      expect(n.error, isNull);
      n.dispose();
    });

    testWidgets('a HAFAS-fallback resolution is reported back distinctly from a '
        'clean resolve', (tester) async {
      final service = _PollService([
        [
          {
            'id': 'seg-1',
            'route_status': 'resolved',
            'route_degraded': false,
            'route_hafas_failed': true,
          }
        ],
      ]);
      final n = _notifier(service);

      final outcome = _await(n.pollSegmentResolution('seg-1'));
      await tester.pump(const Duration(seconds: 5));
      final result = outcome().result!;

      expect(result['route_status'], 'resolved');
      expect(result['degraded'], false);
      expect(result['hafas_failed'], true);
      expect(n.error, isNull);
      n.dispose();
    });

    testWidgets('the provider reason for a HAFAS fallback is reported back',
        (tester) async {
      // Issue #277: the server keeps route_error on the *resolved* segment so
      // the UI can say why the chosen train never resolved.
      final service = _PollService([
        [
          {
            'id': 'seg-1',
            'route_status': 'resolved',
            'route_degraded': true,
            'route_hafas_failed': true,
            'route_error': 'Train lookup failed: HAFAS request failed: 503',
          }
        ],
      ]);
      final n = _notifier(service);

      final outcome = _await(n.pollSegmentResolution('seg-1'));
      await tester.pump(const Duration(seconds: 5));
      final result = outcome().result!;

      expect(result['route_error'], contains('503'));
      n.dispose();
    });

    testWidgets('a clean resolve reports hafas_failed as false', (tester) async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'resolved', 'route_degraded': false}
        ],
      ]);
      final n = _notifier(service);

      final outcome = _await(n.pollSegmentResolution('seg-1'));
      await tester.pump(const Duration(seconds: 5));
      final result = outcome().result!;

      expect(result['hafas_failed'], false);
      n.dispose();
    });

    testWidgets('a failed resolve throws the server reason and marks the tile',
        (tester) async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'failed', 'route_error': 'No route'}
        ],
      ]);
      final n = _notifier(service);

      final outcome = _await(n.pollSegmentResolution('seg-1'));
      await tester.pump(const Duration(seconds: 5));

      expect(outcome().error.toString(), contains('No route'));
      expect(_itemStatus(n, 'seg-1'), 'failed');
      n.dispose();
    });
  });

  group('one poller per trip (issue #278)', () {
    testWidgets('three concurrent resolves, one finishing after 5 minutes, all '
        'reach the map', (tester) async {
      final server = _Server();
      for (final id in ['a', 'b', 'c']) {
        server.segments[id] = _seg(id);
      }
      final n = _notifierOn(server);

      final outcomes = {
        for (final id in ['a', 'b', 'c'])
          id: _await(n.resolveTrainRoute(id)),
      };
      await tester.pump(const Duration(seconds: 10));
      server.resolve('a');
      await tester.pump(const Duration(seconds: 50));
      expect(server.metaCalls, 20,
          reason: 'one /meta every 3 s for all three, not one each');
      server.resolve('b');
      await tester.pump(const Duration(minutes: 4));
      expect(outcomes['c']!().result, isNull);
      expect(_itemStatus(n, 'c'), 'pending');
      server.resolve('c');
      await tester.pump(const Duration(seconds: 15));

      for (final id in ['a', 'b', 'c']) {
        expect(outcomes[id]!().result?['route_status'], 'resolved', reason: id);
        expect(_drawnMode(n, id), 'rail', reason: id);
        expect(_itemStatus(n, id), 'resolved', reason: id);
      }
      expect(n.error, isNull);

      // None once nothing is pending.
      final calls = server.metaCalls;
      await tester.pump(const Duration(minutes: 5));
      expect(server.metaCalls, calls);
    });

    testWidgets('polls every 3 s, then 15 s after 2 minutes, then 60 s after 10',
        (tester) async {
      final server = _Server()..segments['a'] = _seg('a', status: 'pending');
      final n = _notifierOn(server);
      n.pollSegmentResolution('a');

      await tester.pump(const Duration(minutes: 2));
      expect(server.metaCalls, 40);
      await tester.pump(const Duration(minutes: 8));
      expect(server.metaCalls, 40 + 32);
      await tester.pump(const Duration(minutes: 10));
      expect(server.metaCalls, 40 + 32 + 10);

      // A new resolve is quick again.
      server.segments['b'] = _seg('b');
      n.items = [...n.items, _segmentItem(_seg('b'))];
      n.resolveTrainRoute('b');
      await tester.pump(const Duration(seconds: 3));
      expect(server.metaCalls, 40 + 32 + 10 + 1);
      n.dispose();
    });

    testWidgets('a poll already in flight when a resolve is asked for does not '
        'judge it on the state from before', (tester) async {
      final server = _Server()
        ..segments['a'] = _seg('a', status: 'pending')
        // b already carries an older route, which it is about to re-resolve.
        ..segments['b'] = {
          ..._seg('b', status: 'resolved'),
          'route_mode': 'rail',
          'route_polyline': '[[9,9],[8,8]]',
        };
      final n = _notifierOn(server);
      n.pollSegmentResolution('a');

      server.gate = Completer<void>();
      await tester.pump(const Duration(seconds: 3)); // poll sent, held
      final b = _await(n.resolveTrainRoute('b', force: true));
      await tester.pump();
      server.gate!.complete();
      server.gate = null;
      await tester.pump();
      expect(b().result, isNull,
          reason: "the held answer predates b's resolve request");
      expect(_itemStatus(n, 'b'), 'pending');

      server.resolve('b');
      await tester.pump(const Duration(seconds: 3));
      expect(b().result?['route_status'], 'resolved');
      n.dispose();
    });

    testWidgets('a role change mid-poll does not cancel it', (tester) async {
      final server = _Server()..segments['a'] = _seg('a', status: 'pending');
      final n = _notifierOn(server)
        ..ref = const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor');
      final outcome = _await(n.pollSegmentResolution('a'));

      await tester.pump(const Duration(seconds: 3));
      n.ref = const ProjectRef(name: 'Trip', ownerId: 7, role: 'viewer');
      await tester.pump(const Duration(seconds: 3));
      server.resolve('a');
      await tester.pump(const Duration(seconds: 3));

      expect(outcome().result?['route_status'], 'resolved');
      expect(server.metaRefs.last.role, 'viewer',
          reason: 'polls ask as the caller is now');
      n.dispose();
    });

    testWidgets('another trip of the same name stops it', (tester) async {
      final server = _Server()..segments['a'] = _seg('a', status: 'pending');
      final n = _notifierOn(server);
      final outcome = _await(n.pollSegmentResolution('a'));

      await tester.pump(const Duration(seconds: 3));
      final calls = server.metaCalls;
      n.ref = const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor');
      await tester.pump(const Duration(minutes: 1));

      expect(outcome().result?['route_status'], 'cancelled');
      expect(server.metaCalls, calls);
    });

    testWidgets('clear() stops it', (tester) async {
      final server = _Server()..segments['a'] = _seg('a', status: 'pending');
      final n = _notifierOn(server);
      final outcome = _await(n.pollSegmentResolution('a'));

      await tester.pump(const Duration(seconds: 3));
      final calls = server.metaCalls;
      n.clear();
      await tester.pump();
      expect(outcome().result?['route_status'], 'cancelled');

      // Opening the same trip again does not bring the old poll back.
      n.ref = _ref;
      await tester.pump(const Duration(minutes: 1));
      expect(server.metaCalls, calls);
    });

    testWidgets('clear() during a poll in flight drops its answer',
        (tester) async {
      final server = _Server()..segments['a'] = _seg('a', status: 'pending');
      final n = _notifierOn(server);
      n.pollSegmentResolution('a');

      server.resolve('a');
      server.gate = Completer<void>();
      await tester.pump(const Duration(seconds: 3)); // poll sent, held
      n.clear();
      n.ref = _ref;
      n.items = [_segmentItem(_seg('a', status: 'pending'))];
      server.gate!.complete();
      await tester.pump();

      expect(_itemStatus(n, 'a'), 'pending',
          reason: 'an answer for the cleared session is not applied');
    });

    testWidgets('a segment deleted mid-resolve ends its wait', (tester) async {
      final server = _Server()..segments['a'] = _seg('a', status: 'pending');
      final n = _notifierOn(server);
      final outcome = _await(n.pollSegmentResolution('a'));

      server.segments.remove('a');
      await tester.pump(const Duration(seconds: 3));

      expect(outcome().result?['route_status'], 'cancelled');
      final calls = server.metaCalls;
      await tester.pump(const Duration(minutes: 1));
      expect(server.metaCalls, calls, reason: 'nothing left to poll for');
    });
  });
}
