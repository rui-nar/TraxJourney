// Regression tests for issue #283: ProjectNotifier's background loads had no
// real cancellation, just ad hoc `_loadKey != ref` guards. Since ProjectRef.==
// is structural (project_ref.dart), those guards only ever caught navigation
// to a *different* project — a second concurrent call for the *same* ref
// (e.g. the user mashing a Retry button, or an unrelated CRUD mutation
// landing mid-load) compared equal and could still let a stale background
// continuation mutate/notify after a newer one had already taken over.
// project_notifier.dart's _SupersessionTrack closes that gap — three separate
// instances (_loadTrack/_reloadTrack/_detailsOnlyReloadTrack) so that fixing
// the same-ref race doesn't introduce a new one: an unrelated CRUD reload
// must not be able to cancel load()'s own independent progressive fetch.
//
// These tests deliberately race two same-ref calls against each other via
// Completer-controlled fakes, matching the "manual gen/token" testing
// convention already used by build_full_track_background_isolate_test.dart
// (see that file's header) rather than sleeping and hoping for an ordering —
// the whole point of these bugs is that they're about race *ordering*.

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

Map<String, dynamic> _metaNamed(String name) => {
      'name': 'Trip',
      'activities': [
        {'id': '1', 'name': name}
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': '1'}
      ],
      'people': [],
      'groups': [],
    };

Map<String, dynamic> _meta() => _metaNamed('meta');

Map<String, dynamic> _emptyGeo() => {'type': 'FeatureCollection', 'features': <dynamic>[]};

Map<String, dynamic> _detailsNamed(String name) => {
      'activities': [
        {'id': '1', 'name': name}
      ],
    };

/// A [ProjectService] whose `getGeo`/`getDetails` calls are held open on
/// Completers the test resolves by hand, so two concurrent same-ref calls
/// can be interleaved deterministically.
///
/// `getDetailsMeta` resolves immediately by default (so `load()`'s Phase 1
/// isn't blocked by tests that don't care about it) unless [metaCalls] is
/// set to a list, at which point subsequent calls are held on it the same
/// way — a test flips this on only after its own `load()` setup call has
/// already completed, to race two *later* same-ref calls against each other
/// without also blocking `load()` itself.
///
/// `getGeo` is held by default ([holdGeoCalls]) since most tests need
/// `load()`'s own Phase 2 fetch held open; a test driving an unrelated
/// same-ref reload afterward flips this off first so that reload's own geo
/// fetch (fetched fresh, not under test) doesn't also hang forever.
class _RacingService extends ProjectService {
  final List<Completer<Map<String, dynamic>>> geoCalls = [];
  final List<Completer<Map<String, dynamic>>> detailsCalls = [];
  bool holdGeoCalls = true;
  List<Completer<Map<String, dynamic>>>? metaCalls;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) {
    final calls = metaCalls;
    if (calls == null) return Future.value(_meta());
    final c = Completer<Map<String, dynamic>>();
    calls.add(c);
    return c.future;
  }

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async => _emptyGeo();

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) {
    if (!holdGeoCalls) return Future.value(_emptyGeo());
    final c = Completer<Map<String, dynamic>>();
    geoCalls.add(c);
    return c.future;
  }

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) {
    final c = Completer<Map<String, dynamic>>();
    detailsCalls.add(c);
    return c.future;
  }

  @override
  Future<Map<String, dynamic>> resetActivityTrack(ProjectRef ref, int activityId) async => {};
}

/// Test-only subclass exposing the @protected applyFullActivities/
/// currentLoadToken seam ProjectNotifier gives staged-loading subclasses
/// (ViewProjectNotifier, SharedProjectNotifier) — see view_screen.dart /
/// shared_project_screen.dart for the real callers this mirrors.
class _ExposedNotifier extends ProjectNotifier {
  _ExposedNotifier(super.service);

  Future<void> apply(List<Map<String, dynamic>> acts,
          {required ProjectRef ref, required int token}) =>
      applyFullActivities(acts, ref: ref, token: token);

  int get token => currentLoadToken;
}

Future<void> _pumpUntil(bool Function() done, {int maxTicks = 40}) async {
  for (var i = 0; i < maxTicks && !done(); i++) {
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}

void main() {
  setUp(() => projectDataCache.resetForTest());

  test(
      '_loadFullGeoProgressively: a stale load()\'s geo fetch resolving late '
      'does not flip isGeoLoaded for a second concurrent load() of the same '
      'ref (issue #283 bug #1, same-ref case)', () async {
    final svc = _RacingService();
    final notifier = ProjectNotifier(svc);

    // isGeoLoaded defaults to true on the base ProjectNotifier ("so
    // manage-mode screens that use the base load() see no behaviour change"
    // — see its doc comment); only ViewProjectNotifier/SharedProjectNotifier
    // reset it false before calling load() to use it progressively. Mirror
    // that here so this test can observe _loadFullGeoProgressively's write to
    // it, exactly as those subclasses do in production.

    // Call A: Phase 1 completes fast; Phase 2's getGeo() is held on
    // geoCalls[0].
    await notifier.load(_ref);
    notifier.isGeoLoaded = false;
    await _pumpUntil(() => svc.geoCalls.length == 1);
    expect(svc.geoCalls, hasLength(1));
    expect(notifier.isGeoLoaded, isFalse);

    // Call B: a second concurrent load() for the *same* ref — the exact
    // "mashing Retry" scenario the issue describes. Its own Phase 2 getGeo()
    // is held on geoCalls[1].
    await notifier.load(_ref);
    notifier.isGeoLoaded = false;
    await _pumpUntil(() => svc.geoCalls.length == 2);
    expect(svc.geoCalls, hasLength(2));
    expect(notifier.isGeoLoaded, isFalse);

    // Resolve A's (stale) fetch first. Before the fix, `_loadKey == ref`
    // still held (same ref!) so this alone used to flip isGeoLoaded even
    // though B — the call actually in flight now — hasn't delivered anything.
    svc.geoCalls[0].complete(_emptyGeo());
    await _pumpUntil(() => false, maxTicks: 5); // let the continuation run
    expect(notifier.isGeoLoaded, isFalse,
        reason: 'a stale same-ref load must not be able to mark geo loaded '
            'for the call that actually superseded it');

    // Now resolve B's (current) fetch — this is the one that should count.
    svc.geoCalls[1].complete(_emptyGeo());
    await _pumpUntil(() => notifier.isGeoLoaded);
    expect(notifier.isGeoLoaded, isTrue);

    // Both geo fetches finishing chains into _loadElevationData's own
    // getDetails() call (via whenComplete) — drain those too so nothing is
    // left hanging past the end of the test.
    await _pumpUntil(() => svc.detailsCalls.length == 2);
    for (final c in svc.detailsCalls) {
      if (!c.isCompleted) c.complete(_detailsNamed('drained'));
    }
    await _pumpUntil(() => false, maxTicks: 5);
  });

  test(
      'an unrelated reloadDetailsOnly() (reorder/sort/trip-date edit) does '
      'not cancel a load()\'s still in-flight progressive full-geo fetch '
      '(issue #283 review finding)', () async {
    final svc = _RacingService();
    final notifier = ProjectNotifier(svc);

    // load()'s Phase 2 geo fetch is held open — it is genuinely still in
    // flight for a large trip when the details-only reload below fires.
    await notifier.load(_ref);
    notifier.isGeoLoaded = false;
    await _pumpUntil(() => svc.geoCalls.length == 1);

    // A completely unrelated mutation (e.g. the user drag-reordering an
    // activity, which is available as soon as Phase 1's low-res data has
    // rendered) triggers a details-only reload. Before the fix, this shared
    // load()'s own supersession track, so bumping it here would silently and
    // permanently cancel the still-in-flight progressive geo fetch below —
    // isGeoLoaded would never become true again for the rest of the session.
    await notifier.reloadDetailsOnly(_ref);

    // The original load()'s geo fetch now resolves. It must still be able to
    // land normally — the unrelated details-only reload must not have
    // superseded it.
    svc.geoCalls[0].complete(_emptyGeo());
    await _pumpUntil(() => notifier.isGeoLoaded);
    expect(notifier.isGeoLoaded, isTrue,
        reason: 'an unrelated details-only reload must not be able to '
            'starve an in-flight load()\'s own progressive geo fetch, which '
            'has no other way to ever complete');

    // Drain the whenComplete-chained _loadElevationData details fetch.
    await _pumpUntil(() => svc.detailsCalls.isNotEmpty);
    for (final c in svc.detailsCalls) {
      if (!c.isCompleted) c.complete(_detailsNamed('drained'));
    }
    await _pumpUntil(() => false, maxTicks: 5);
  });

  test(
      'an unrelated _silentReload (e.g. resetActivityTrack) does not cancel '
      'a load()\'s still in-flight progressive full-geo fetch (issue #283 '
      'review finding)', () async {
    final svc = _RacingService();
    final notifier = ProjectNotifier(svc);

    // load()'s Phase 2 geo fetch is held open — it is genuinely still in
    // flight for a large trip when the unrelated CRUD reload below fires.
    await notifier.load(_ref);
    notifier.isGeoLoaded = false;
    await _pumpUntil(() => svc.geoCalls.length == 1);

    // A completely unrelated mutation (resetting an activity's track)
    // triggers _silentReload. Its own geo fetch resolves immediately
    // (holdGeoCalls flipped off here) so this await completes; before this
    // fix, _silentReload shared load()'s own supersession track, so bumping
    // it here would silently and permanently cancel the still-in-flight
    // progressive geo fetch below — isGeoLoaded (and the low-res-to-full-res
    // map/elevation-chart upgrade it gates) would never land for the rest of
    // the session.
    svc.holdGeoCalls = false;
    await notifier.resetActivityTrack(1);

    // The original load()'s geo fetch now resolves. It must still be able to
    // land normally — the unrelated _silentReload must not have superseded
    // it.
    svc.geoCalls[0].complete(_emptyGeo());
    await _pumpUntil(() => notifier.isGeoLoaded);
    expect(notifier.isGeoLoaded, isTrue,
        reason: 'an unrelated _silentReload (CRUD mutation) must not be '
            'able to starve an in-flight load()\'s own progressive geo '
            'fetch, which has no other way to ever complete');

    // Drain the whenComplete-chained _loadElevationData details fetch.
    await _pumpUntil(() => svc.detailsCalls.isNotEmpty);
    for (final c in svc.detailsCalls) {
      if (!c.isCompleted) c.complete(_detailsNamed('drained'));
    }
    await _pumpUntil(() => false, maxTicks: 5);
  });

  test(
      'reloadDetailsOnly: a stale call\'s late-arriving response does not '
      'overwrite a newer concurrent reloadDetailsOnly() for the same ref '
      '(issue #283 review finding — mutation must be guarded, not just the '
      'trailing notify)', () async {
    final svc = _RacingService();
    final notifier = ProjectNotifier(svc);

    await notifier.load(_ref);
    expect(notifier.activities.first['name'], 'meta');

    // Switch to holding getDetailsMeta() so the two reloadDetailsOnly()
    // calls below can be raced deterministically against each other —
    // load()'s own Phase 1 fetch above already completed before this flips.
    svc.metaCalls = [];

    // Call A: starts, its getDetailsMeta() is held on metaCalls[0].
    final callA = notifier.reloadDetailsOnly(_ref);
    await _pumpUntil(() => svc.metaCalls!.length == 1);

    // Call B: a second concurrent reloadDetailsOnly() for the same ref.
    final callB = notifier.reloadDetailsOnly(_ref);
    await _pumpUntil(() => svc.metaCalls!.length == 2);

    // B's (current) response lands first with the real data.
    svc.metaCalls![1].complete(_metaNamed('CURRENT'));
    await callB;
    expect(notifier.activities.first['name'], 'CURRENT');

    // A's (stale) response lands late. Before this fix,
    // _silentReloadDetailsOnly guarded only its own trailing notify/error,
    // not the _applyDetails mutation itself — so this would have silently
    // overwritten 'CURRENT' with 'STALE' even though only the notify was
    // (correctly) suppressed, exactly the bug class applyFullActivities was
    // fixed for.
    svc.metaCalls![0].complete(_metaNamed('STALE'));
    await callA;
    expect(notifier.activities.first['name'], 'CURRENT',
        reason: 'a stale reloadDetailsOnly() call must not be able to '
            'overwrite a newer one\'s already-applied data');

    // Drain load()'s still-pending Phase 2 fetches (not under test here).
    for (final c in svc.geoCalls) {
      if (!c.isCompleted) c.complete(_emptyGeo());
    }
    await _pumpUntil(() => false, maxTicks: 5);
    for (final c in svc.detailsCalls) {
      if (!c.isCompleted) c.complete(_detailsNamed('drained'));
    }
    await _pumpUntil(() => false, maxTicks: 5);
  });

  test(
      '_loadElevationData: a stale load()\'s details fetch resolving late '
      'does not overwrite activities merged by a second concurrent load() of '
      'the same ref (issue #283, same-ref case)', () async {
    final svc = _RacingService();
    final notifier = ProjectNotifier(svc);

    // Call A: Phase 2's getGeo() resolves immediately (not under test here);
    // its getDetails() (feeding _loadElevationData) is held on detailsCalls[0].
    await notifier.load(_ref);
    await _pumpUntil(() => svc.geoCalls.isNotEmpty);
    svc.geoCalls[0].complete(_emptyGeo());
    await _pumpUntil(() => svc.detailsCalls.length == 1);
    expect(svc.detailsCalls, hasLength(1));

    // Call B: second concurrent load() for the same ref.
    await notifier.load(_ref);
    await _pumpUntil(() => svc.geoCalls.length == 2);
    svc.geoCalls[1].complete(_emptyGeo());
    await _pumpUntil(() => svc.detailsCalls.length == 2);
    expect(svc.detailsCalls, hasLength(2));

    // B's (current) details land first with the real data.
    svc.detailsCalls[1].complete(_detailsNamed('CURRENT'));
    await _pumpUntil(() => notifier.activities.first['name'] == 'CURRENT');
    expect(notifier.activities.first['name'], 'CURRENT');

    // A's (stale) details land late. Pre-fix, `_loadKey != ref` was the only
    // guard here and — same ref — it wouldn't have caught this: the merge
    // would silently overwrite 'CURRENT' with 'STALE'.
    svc.detailsCalls[0].complete(_detailsNamed('STALE'));
    await _pumpUntil(() => false, maxTicks: 10);
    expect(notifier.activities.first['name'], 'CURRENT',
        reason: 'the stale same-ref load\'s details fetch must not clobber '
            'the current load\'s already-merged activities');
  });

  test(
      'applyFullActivities: rejects a token captured before a load() that '
      'has since been superseded, even for the same ref (issue #283 bug #3)',
      () async {
    // _RacingService (not the default ProjectService(), which would hit the
    // real network via the global `api`) — its Phase 2 fetches are drained at
    // the end below; this test only cares about the token load() bumps.
    final svc = _RacingService();
    final notifier = _ExposedNotifier(svc);
    await notifier.load(_ref);
    notifier.activities = [
      {'id': '1', 'name': 'OLD'}
    ];
    final staleToken = notifier.token;

    // A second concurrent load() for the *same* ref supersedes staleToken —
    // currentLoadKey would still equal the ref (structural equality), so
    // only the token can tell these two calls apart.
    await notifier.load(_ref);
    notifier.activities = [
      {'id': '1', 'name': 'CURRENT'}
    ];

    // Applying with the stale token must be rejected outright — not even the
    // activities merge should run (bug #3: it used to mutate unconditionally
    // and only skip the trailing notify).
    await notifier.apply([
      {'id': '1', 'name': 'STALE'}
    ], ref: _ref, token: staleToken);
    expect(notifier.activities.first['name'], 'CURRENT');

    // Applying with the current token still works normally.
    await notifier.apply([
      {'id': '1', 'name': 'FRESH'}
    ], ref: _ref, token: notifier.token);
    expect(notifier.activities.first['name'], 'FRESH');

    // Drain both load() calls' still-pending Phase 2 fetches so nothing is
    // left hanging past the end of the test. getGeo() first — getDetails()
    // is only requested once _loadFullGeoProgressively finishes (chained via
    // whenComplete), so its completers don't exist yet at this point.
    for (final c in svc.geoCalls) {
      if (!c.isCompleted) c.complete(_emptyGeo());
    }
    await _pumpUntil(() => false, maxTicks: 5);
    for (final c in svc.detailsCalls) {
      if (!c.isCompleted) c.complete(_detailsNamed('drained'));
    }
    await _pumpUntil(() => false, maxTicks: 5);
  });
}
