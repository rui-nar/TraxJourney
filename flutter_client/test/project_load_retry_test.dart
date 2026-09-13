// Automatic recovery of a slow project load (issue #178).
//
// The activity panel blocks on ProjectNotifier.load()'s first two requests. When
// one of them missed its deadline — a cold load of a large trip took 11-13 s
// server-side before the body even started moving — `items` stayed empty and the
// panel showed the raw
// "Error: TimeoutException after 0:00:30.000000: Future not completed"
// with no way back except reloading the whole app. The map, meanwhile, filled in
// fine, because the background geo phase has always retried once.
//
// Covers:
//  - a transient failure is retried automatically and the panel fills in;
//  - both requests are still fired in parallel, and only the failing one is
//    re-fetched;
//  - retries are bounded, and what survives them is a sentence, not a Dart
//    exception;
//  - a 4xx refusal is surfaced at once rather than retried into a long wait;
//  - navigating away mid-retry abandons the load instead of writing an error
//    over the project the user actually opened;
//  - the elevation fetch waits for the geo phase rather than racing it.

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');
const _fast = [Duration(milliseconds: 1), Duration(milliseconds: 1)];

Map<String, dynamic> _details(String name) => {
      'name': name,
      'activities': [
        {'id': 111, 'name': 'Ride', 'type': 'Ride'},
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': 111},
      ],
    };

Map<String, dynamic> _emptyGeo() =>
    {'type': 'FeatureCollection', 'features': <dynamic>[]};

/// Fails the first [metaFailures] /meta calls (and [lowResFailures] low-res
/// calls) with [error], then serves a real payload. Counts every call.
class _FlakyService extends ProjectService {
  _FlakyService({
    this.metaFailures = 0,
    this.lowResFailures = 0,
    this.geoFailures = 0,
    this.failOnly,
    Object? error,
  }) : error = error ?? TimeoutException('after 0:00:60.000000', const Duration(seconds: 60));

  final int metaFailures;
  final int lowResFailures;
  final int geoFailures;

  /// Project name to fail for; null fails for every project.
  final String? failOnly;
  final Object error;

  bool _shouldFail(ProjectRef ref, int callNo, int failures) =>
      callNo <= failures && (failOnly == null || ref.name == failOnly);

  int metaCalls = 0;
  int lowResCalls = 0;
  int geoCalls = 0;
  int detailsCalls = 0;
  final events = <String>[];

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    metaCalls++;
    if (_shouldFail(ref, metaCalls, metaFailures)) throw error;
    return _details(ref.name);
  }

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async {
    lowResCalls++;
    if (_shouldFail(ref, lowResCalls, lowResFailures)) throw error;
    return _emptyGeo();
  }

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async {
    geoCalls++;
    if (_shouldFail(ref, geoCalls, geoFailures)) throw error;
    events.add('geo:start');
    await Future<void>.delayed(const Duration(milliseconds: 5));
    events.add('geo:done');
    return _emptyGeo();
  }

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async {
    detailsCalls++;
    events.add('details:start');
    return _details(ref.name);
  }
}

ProjectNotifier _notifier(ProjectService service) =>
    ProjectNotifier(service)..loadRetryBackoff = _fast;

void main() {
  test('a transient failure is retried and the panel fills in', () async {
    final service = _FlakyService(metaFailures: 1);
    final notifier = _notifier(service);

    await notifier.load(_ref);

    expect(notifier.error, isNull, reason: 'a retried success is not an error');
    expect(notifier.items, hasLength(1), reason: 'the panel has its items');
    expect(service.metaCalls, 2, reason: 'one failure, one retry');
  });

  test('only the request that failed is re-fetched', () async {
    final service = _FlakyService(metaFailures: 2);
    final notifier = _notifier(service);

    await notifier.load(_ref);

    expect(notifier.items, hasLength(1));
    expect(service.metaCalls, 3);
    expect(service.lowResCalls, 1,
        reason: 'the low-res geo arrived first time and is not re-fetched');
  });

  test('a failed low-res geo is retried too, so the map still gets its lines',
      () async {
    final service = _FlakyService(lowResFailures: 1);
    final notifier = _notifier(service);

    await notifier.load(_ref);

    expect(notifier.error, isNull);
    expect(service.lowResCalls, 2);
    expect(service.metaCalls, 1, reason: 'the healthy request is not re-fetched');
    expect(notifier.geo, isNotNull);
  });

  test('retries are bounded and the survivor is a sentence, not an exception',
      () async {
    final service = _FlakyService(metaFailures: 99);
    final notifier = _notifier(service);

    await notifier.load(_ref);

    expect(service.metaCalls, 3, reason: 'first attempt plus two backoffs');
    expect(notifier.items, isEmpty);
    expect(notifier.error, isNotNull);
    expect(notifier.error, isNot(contains('TimeoutException')));
    expect(notifier.error, isNot(contains('Future not completed')));
    expect(notifier.error, contains('reopen'));
  });

  test(
      'a persistent full-res geo timeout is a sentence, not an exception '
      '(issue #190)', () async {
    // Opening a trip: phase 1 (meta + low-res) succeeds, so the map renders,
    // but phase 2 (full-res geo) times out on both attempts. That background
    // failure used to write the raw `TimeoutException ...: Future not
    // completed` into `error`, which the activity panel shows verbatim.
    final service = _FlakyService(geoFailures: 99);
    final notifier = _notifier(service);
    final done = Completer<void>();
    notifier.addListener(() {
      if (notifier.error != null && !done.isCompleted) done.complete();
    });

    await notifier.load(_ref);
    await done.future.timeout(const Duration(seconds: 5));

    expect(notifier.error, isNotNull);
    expect(notifier.error, isNot(contains('TimeoutException')));
    expect(notifier.error, isNot(contains('Future not completed')));
    expect(notifier.isGeoLoaded, isFalse);
  });

  test('a refusal reaches the user verbatim, and immediately', () async {
    final service = _FlakyService(
      metaFailures: 99,
      error: ApiException(403, '{"detail":"You need editor access to do this"}'),
    );
    final notifier = _notifier(service);

    await notifier.load(_ref);

    expect(notifier.error, 'You need editor access to do this');
    expect(notifier.loadErrorStatus, 403);
    expect(service.metaCalls, 1,
        reason: "a 4xx is the server's answer — retrying only delays it");
  });

  test('a 5xx is retried — that one really can pass', () async {
    final service = _FlakyService(
      metaFailures: 1,
      error: ApiException(503, '{"detail":"Service Unavailable"}'),
    );
    final notifier = _notifier(service);

    await notifier.load(_ref);

    expect(notifier.error, isNull);
    expect(service.metaCalls, 2);
  });

  test('the panel stays in its loading state while retrying', () async {
    final service = _FlakyService(metaFailures: 1);
    final notifier = _notifier(service);
    final loadingDuringNotifications = <bool>[];
    notifier.addListener(() => loadingDuringNotifications.add(notifier.isLoading));

    final pending = notifier.load(_ref);
    expect(notifier.isLoading, isTrue);
    await pending;

    expect(loadingDuringNotifications.first, isTrue,
        reason: 'the spinner is what the user sees while the retry runs');
    expect(notifier.isLoading, isFalse);
  });

  test('navigating away mid-retry does not write an error over the new project',
      () async {
    // Only the abandoned project fails, so an error afterwards can only have
    // come from the load the user walked away from.
    final service = _FlakyService(metaFailures: 99, failOnly: 'Trip');
    final notifier = _notifier(service)
      ..loadRetryBackoff = const [Duration(milliseconds: 20)];

    final first = notifier.load(_ref);
    // Land the navigation while the first load is still backing off.
    await Future<void>.delayed(const Duration(milliseconds: 5));
    await notifier.load(const ProjectRef(name: 'Other'));
    await first;

    expect(notifier.ref?.name, 'Other');
    expect(notifier.error, isNull,
        reason: "the abandoned load's failure belongs to a project the user left");
  });

  test('the elevation fetch waits for the geo phase instead of racing it',
      () async {
    final service = _FlakyService();
    final notifier = _notifier(service);

    await notifier.load(_ref);
    await Future<void>.delayed(const Duration(milliseconds: 30));

    expect(service.events, ['geo:start', 'geo:done', 'details:start'],
        reason: 'three whole-project loads at once is what starved the server');
  });
}
