// The degraded-route upgrade watch (issue #207).
//
// sweep_degraded_segments() retries a degraded (straight-line) segment on its
// own hourly schedule, independent of whether anyone has the project open.
// This is the client's only way to learn that happened for a project that's
// already loaded: a periodic + best-effort check against /meta, comparing the
// count of currently-degraded segments against what it last saw. It must
// never apply the fresher data itself — only flag that it exists — so a
// background tick can't rewrite what someone is actively looking at or
// mid-edit on. Reload/Later are the only two paths that touch state.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

Map<String, dynamic> _segment(String id, {required bool degraded}) => {
      'id': id,
      'route_status': 'resolved',
      'route_degraded': degraded,
    };

Map<String, dynamic> _meta(List<Map<String, dynamic>> segments) => {
      'name': 'Trip',
      'activities': <dynamic>[],
      'items': [
        for (final s in segments) {'item_type': 'segment', 'segment': s},
      ],
      'people': <dynamic>[],
      'groups': <dynamic>[],
    };

class _ScriptedMetaService extends ProjectService {
  _ScriptedMetaService(this.metaSequence);

  final List<Map<String, dynamic>> metaSequence;
  int calls = 0;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    final idx = calls < metaSequence.length ? calls : metaSequence.length - 1;
    calls++;
    return metaSequence[idx];
  }

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service);
  @override
  bool get loadOwnerExtras => false;
}

void main() {
  group('checkDegradedRouteUpgrade', () {
    test('the first check only establishes a baseline, no banner', () async {
      final service = _ScriptedMetaService([
        _meta([_segment('a', degraded: true)]),
      ]);
      final n = _TestProjectNotifier(service)..ref = _ref;

      await n.checkDegradedRouteUpgrade(_ref);

      expect(n.degradedRouteUpgradeAvailable, isFalse);
    });

    test('a drop in the degraded count shows the banner', () async {
      final service = _ScriptedMetaService([
        _meta([_segment('a', degraded: true), _segment('b', degraded: true)]),
        _meta([_segment('a', degraded: true), _segment('b', degraded: false)]),
      ]);
      final n = _TestProjectNotifier(service)..ref = _ref;

      await n.checkDegradedRouteUpgrade(_ref); // baseline: 2
      await n.checkDegradedRouteUpgrade(_ref); // now: 1 → upgrade

      expect(n.degradedRouteUpgradeAvailable, isTrue);
    });

    test('an unchanged degraded count never shows the banner', () async {
      final service = _ScriptedMetaService([
        _meta([_segment('a', degraded: true)]),
        _meta([_segment('a', degraded: true)]),
        _meta([_segment('a', degraded: true)]),
      ]);
      final n = _TestProjectNotifier(service)..ref = _ref;

      await n.checkDegradedRouteUpgrade(_ref);
      await n.checkDegradedRouteUpgrade(_ref);
      await n.checkDegradedRouteUpgrade(_ref);

      expect(n.degradedRouteUpgradeAvailable, isFalse);
    });

    test('a fresh degrade (count increases) never shows the banner', () async {
      final service = _ScriptedMetaService([
        _meta([_segment('a', degraded: false)]),
        _meta([_segment('a', degraded: false), _segment('b', degraded: true)]),
      ]);
      final n = _TestProjectNotifier(service)..ref = _ref;

      await n.checkDegradedRouteUpgrade(_ref); // baseline: 0
      await n.checkDegradedRouteUpgrade(_ref); // now: 1 — a new degrade, not an upgrade

      expect(n.degradedRouteUpgradeAvailable, isFalse);
    });

    test('navigating to another project cancels the check', () async {
      final service = _ScriptedMetaService([
        _meta([_segment('a', degraded: true)]),
      ]);
      final n = _TestProjectNotifier(service)..ref = _ref;

      n.ref = const ProjectRef(name: 'Other trip');
      await n.checkDegradedRouteUpgrade(_ref);

      expect(service.calls, 0);
    });

    test('a transient fetch error is ignored, not treated as an upgrade',
        () async {
      final n = _TestProjectNotifier(_ThrowingMetaService())..ref = _ref;

      await n.checkDegradedRouteUpgrade(_ref);

      expect(n.degradedRouteUpgradeAvailable, isFalse);
    });
  });

  group('dismissDegradedRouteUpgrade / reloadForDegradedUpgrade', () {
    test('dismiss hides the banner without touching project data', () async {
      final service = _ScriptedMetaService([
        _meta([_segment('a', degraded: true)]),
        _meta([_segment('a', degraded: false)]),
      ]);
      final n = _TestProjectNotifier(service)..ref = _ref;
      await n.checkDegradedRouteUpgrade(_ref);
      await n.checkDegradedRouteUpgrade(_ref);
      expect(n.degradedRouteUpgradeAvailable, isTrue);

      n.dismissDegradedRouteUpgrade();

      expect(n.degradedRouteUpgradeAvailable, isFalse);
      expect(service.calls, 2, reason: 'dismiss must not fetch anything');
    });

    test('reload clears the banner and re-fetches the project', () async {
      final service = _ScriptedMetaService([
        _meta([_segment('a', degraded: true)]),
      ]);
      final n = _TestProjectNotifier(service)..ref = _ref;
      // Simulate the banner already being up (as the owning screen's
      // startDegradedRouteWatch would have done) without a real check here.
      n.degradedRouteUpgradeAvailable = true;

      await n.reloadForDegradedUpgrade();

      expect(n.degradedRouteUpgradeAvailable, isFalse);
      expect(service.calls, greaterThanOrEqualTo(1),
          reason: 'reload must re-fetch, not just clear the flag');
    });
  });
}

class _ThrowingMetaService extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    throw Exception('network blip');
  }
}
