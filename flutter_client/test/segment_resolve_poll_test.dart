// Segment route-resolve polling (issue #213).
//
// `pollSegmentResolution` is the only code path that syncs a segment's
// `route_status` back into client state after a resolve is triggered. Before
// this, a poll that never saw a terminal status within its own deadline gave
// up silently — no error, and nothing else in the app ever re-checked that
// segment, so the tile's spinner was stranded until the project happened to
// be reloaded from scratch. Mirrors `_pollActivityRefresh`'s existing timeout
// handling (see activity_refresh_poll_test.dart).

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');
const _fast = Duration(milliseconds: 1);

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

ProjectNotifier _notifier(ProjectService service, {String segId = 'seg-1'}) {
  final n = ProjectNotifier(service)..ref = _ref;
  n.items = [
    _segmentItem({'id': segId, 'route_status': 'pending'}),
  ];
  return n;
}

void main() {
  group('pollSegmentResolution', () {
    test('a poll that never sees a terminal status sets an error instead of '
        'leaving the tile stranded (issue #213)', () async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'pending'}
        ],
      ]);
      final n = _notifier(service);

      final result = await n.pollSegmentResolution('seg-1',
          interval: _fast, timeout: const Duration(milliseconds: 30));

      expect(result['route_status'], 'pending');
      expect(n.error, contains('taking longer than expected'));
    });

    test('resolving before the deadline clears any risk of a stale error',
        () async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'resolved', 'route_degraded': false}
        ],
      ]);
      final n = _notifier(service);

      final result = await n.pollSegmentResolution('seg-1',
          interval: _fast, timeout: const Duration(seconds: 5));

      expect(result['route_status'], 'resolved');
      expect(result['degraded'], false);
      expect(n.error, isNull);
    });

    test('a degraded resolution is reported back as resolved, not failed',
        () async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'resolved', 'route_degraded': true}
        ],
      ]);
      final n = _notifier(service);

      final result = await n.pollSegmentResolution('seg-1',
          interval: _fast, timeout: const Duration(seconds: 5));

      expect(result['route_status'], 'resolved');
      expect(result['degraded'], true);
      expect(n.error, isNull);
    });

    test('a HAFAS-fallback resolution is reported back distinctly from a '
        'clean resolve', () async {
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

      final result = await n.pollSegmentResolution('seg-1',
          interval: _fast, timeout: const Duration(seconds: 5));

      expect(result['route_status'], 'resolved');
      expect(result['degraded'], false);
      expect(result['hafas_failed'], true);
      expect(n.error, isNull);
    });

    test('the provider reason for a HAFAS fallback is reported back', () async {
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

      final result = await n.pollSegmentResolution('seg-1',
          interval: _fast, timeout: const Duration(seconds: 5));

      expect(result['route_error'], contains('503'));
    });

    test('a clean resolve reports hafas_failed as false', () async {
      final service = _PollService([
        [
          {'id': 'seg-1', 'route_status': 'resolved', 'route_degraded': false}
        ],
      ]);
      final n = _notifier(service);

      final result = await n.pollSegmentResolution('seg-1',
          interval: _fast, timeout: const Duration(seconds: 5));

      expect(result['hafas_failed'], false);
    });
  });
}
