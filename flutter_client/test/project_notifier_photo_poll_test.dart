// Post-import photo polling (startPhotoPolling / _refreshMemoryPhotos).
//
// Two regressions live here.
//
// It used to mutate `items[i]` in place and keep the same List instance.
// MapPanel's marker cache only recomputes on `!identical(items, _lastItems)`,
// so that in-place mutation was invisible to it — a memory that had 0 photos
// when the map first built stayed a black marker forever, no matter how many
// photos the poller found. Fixed by giving `items` a new list identity
// whenever the poller actually changes something.
//
// And it used to ask its one small question — has a photo landed yet? — by
// calling `getDetails(bypassCache: true)`, the full project payload, which
// measures 36 MB on a 180-day trip, up to sixty times over three minutes
// (issue #308). It now polls `getMemoryPhotos`, which carries the photo uuids
// and nothing else.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');
const _otherRef = ProjectRef(name: 'Other Trip');

Map<String, dynamic> _memoryItem(String id, List<String> photos) => {
      'item_type': 'memory',
      'memory': {'id': id, 'name': 'A day out', 'photos': photos},
    };

class _PhotoPollService extends ProjectService {
  _PhotoPollService(this.freshPhotos);
  final List<String> freshPhotos;
  int calls = 0;

  @override
  Future<Map<String, List<String>>> getMemoryPhotos(ProjectRef ref) async {
    calls++;
    return {'mem-1': freshPhotos};
  }

  /// The payload the poll must never reach for again — see issue #308. Failing
  /// loudly rather than counting: a poll that fetches this is not a slower
  /// poll, it is the bug back.
  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
      {bool bypassCache = false}) async {
    throw StateError('photo polling must not fetch the full details payload');
  }
}

void main() {
  test('the poll asks for photo uuids, not the full details payload', () async {
    final service = _PhotoPollService(['a', 'b']);
    final notifier = ProjectNotifier(service)
      ..ref = _ref
      ..items = [_memoryItem('mem-1', const [])];
    addTearDown(notifier.dispose);

    notifier.startPhotoPolling(_ref,
        interval: const Duration(milliseconds: 1), maxTicks: 1);
    await Future.delayed(const Duration(milliseconds: 50));

    // getDetails throws if called, and _refreshMemoryPhotos swallows its own
    // errors — so the proof is that the merge still happened.
    expect(service.calls, greaterThanOrEqualTo(1));
    expect((notifier.items[0]['memory'] as Map)['photos'], ['a', 'b']);
  });

  test('a memory goes from no photo to a thumbnail-able uuid', () async {
    // A count would be enough to notice the change and useless to act on it:
    // the marker builds its thumbnail URL from `photos.first`
    // (map_panel.dart), so the uuid itself has to arrive.
    final service = _PhotoPollService(['uuid-late']);
    final notifier = ProjectNotifier(service)
      ..ref = _ref
      ..items = [_memoryItem('mem-1', const [])];
    addTearDown(notifier.dispose);

    notifier.startPhotoPolling(_ref,
        interval: const Duration(milliseconds: 1), maxTicks: 1);
    await Future.delayed(const Duration(milliseconds: 50));

    final mem = notifier.items[0]['memory'] as Map;
    expect(mem['photos'], ['uuid-late']);
    // The rest of the memory survives the swap — the poll updates photos, it
    // does not replace the item with a photos-only stub.
    expect(mem['name'], 'A day out');
  });

  test('a photo-count change gives items a new list identity', () async {
    final service = _PhotoPollService(['a', 'b']);
    final notifier = ProjectNotifier(service)
      ..ref = _ref
      ..items = [_memoryItem('mem-1', const [])];
    addTearDown(notifier.dispose);

    final originalItems = notifier.items;

    notifier.startPhotoPolling(_ref,
        interval: const Duration(milliseconds: 1), maxTicks: 1);
    // Let the timer tick and its async body (the fetch + notifyListeners) run.
    await Future.delayed(const Duration(milliseconds: 50));

    expect(service.calls, greaterThanOrEqualTo(1));
    expect(identical(notifier.items, originalItems), isFalse,
        reason: 'a same-object mutation would leave MapPanel\'s '
            'identical(items, _lastItems) cache check blind to the change');
    final mem = notifier.items[0]['memory'] as Map;
    expect(mem['photos'], ['a', 'b']);
  });

  test('no photo-count change leaves items untouched', () async {
    final service = _PhotoPollService(['a']);
    final notifier = ProjectNotifier(service)
      ..ref = _ref
      ..items = [_memoryItem('mem-1', const ['a'])];
    addTearDown(notifier.dispose);

    final originalItems = notifier.items;

    notifier.startPhotoPolling(_ref,
        interval: const Duration(milliseconds: 1), maxTicks: 1);
    await Future.delayed(const Duration(milliseconds: 50));

    expect(identical(notifier.items, originalItems), isTrue);
  });

  test('polling stops at maxTicks', () async {
    final service = _PhotoPollService(['a']);
    final notifier = ProjectNotifier(service)
      ..ref = _ref
      ..items = [_memoryItem('mem-1', const [])];
    addTearDown(notifier.dispose);

    notifier.startPhotoPolling(_ref,
        interval: const Duration(milliseconds: 1), maxTicks: 3);
    await Future.delayed(const Duration(milliseconds: 200));

    expect(service.calls, 3);
  });

  test('polling stops when the notifier moves to another project', () async {
    final service = _PhotoPollService(['a']);
    final notifier = ProjectNotifier(service)
      ..ref = _ref
      ..items = [_memoryItem('mem-1', const [])];
    addTearDown(notifier.dispose);

    // A budget far larger than this test's wall clock, so what stops the timer
    // can only be the ref check and never maxTicks running out.
    notifier.startPhotoPolling(_ref,
        interval: const Duration(milliseconds: 5), maxTicks: 1000);
    await Future.delayed(const Duration(milliseconds: 40));
    expect(service.calls, greaterThan(0));

    notifier.ref = _otherRef;
    await Future.delayed(const Duration(milliseconds: 20));
    final settled = service.calls;
    await Future.delayed(const Duration(milliseconds: 100));

    expect(service.calls, settled,
        reason: 'the previous project\'s poll must not outlive the switch');
  });

  test('default poll budget outlives the old 20-tick (60s) cutoff', () async {
    // A large bulk import's slower photo downloads used to outlast the old
    // 60s window (maxTicks: 20 @ 3s) with no further signal once it gave up.
    // Only the interval is overridden here (for test speed) — maxTicks is
    // left at its production default so this proves the widened budget
    // itself, not a test-only value.
    final service = _PhotoPollService(['a']);
    final notifier = ProjectNotifier(service)
      ..ref = _ref
      ..items = [_memoryItem('mem-1', const [])];
    addTearDown(notifier.dispose);

    notifier.startPhotoPolling(_ref, interval: const Duration(milliseconds: 1));
    // The old default would have stopped after 20 ticks; wait well past that
    // (with margin for slow/CI timer granularity) to prove the timer is
    // still polling under the new budget.
    await Future.delayed(const Duration(milliseconds: 250));

    expect(service.calls, greaterThan(20));
  });
}
