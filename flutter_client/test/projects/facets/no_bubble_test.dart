// The root stops re-notifying facet changes (issue #294, unit U19 of
// docs/CLIENT_STATE_MAP_PLAN.md, Decision 17).
//
// ProjectNotifier.notifyListeners() flushes the facets written since the last
// notify, then notifies its own listeners only when root state — a field not
// in any facet — changed. So selecting a day tells the selection facet's
// listeners and nobody else: the screens that listen to the root, and the
// map's polyline geometry under them, are left alone.

import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/members_service.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Every notification [n] and its facets send, in order, by name.
List<String> _record(ProjectNotifier n) {
  final heard = <String>[];
  n.addListener(() => heard.add('root'));
  n.geoFacet.addListener(() => heard.add('geo'));
  n.selectionFacet.addListener(() => heard.add('selection'));
  n.styleFacet.addListener(() => heard.add('style'));
  n.itemsFacet.addListener(() => heard.add('items'));
  n.elevationFacet.addListener(() => heard.add('elevation'));
  return heard;
}

/// Two activities on different days, far apart: Innsbruck (day 1) and Lisbon
/// (day 2).
Map<String, dynamic> _geo() => {
      'type': 'FeatureCollection',
      'features': [
        for (final a in [
          ['1', 11.35, 47.25, 11.55, 47.35],
          ['2', -9.20, 38.70, -9.10, 38.75],
        ])
          {
            'type': 'Feature',
            'properties': {'type': 'activity', 'activity_id': a[0]},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [a[1], a[2]],
                [a[3], a[4]],
              ],
            },
          },
      ],
    };

/// A notifier with a trip's content, whose root has no change pending.
ProjectNotifier _loaded() {
  final n = ProjectNotifier(ProjectService())
    ..ref = const ProjectRef(name: 'Trip')
    ..geoFacetWriter.replaceKeepingLod(_geo())
    ..itemsFacetWriter.setActivities([
      {'id': '1', 'start_date_local': '2026-05-01T08:00:00'},
      {'id': '2', 'start_date_local': '2026-05-02T08:00:00'},
    ])
    ..itemsFacetWriter.setItems([
      {'item_type': 'activity', 'activity_id': '1'},
      {'item_type': 'activity', 'activity_id': '2'},
    ]);
  // As a load ends: one notify for everything it wrote.
  n.notifyListeners();
  return n;
}

const List<(double, GeoPoint)> _track = [
  (0.0, (lat: 47.25, lon: 11.35)),
  (1.0, (lat: 47.35, lon: 11.55)),
];

const _quotaBody = '{"detail": "Over the limit.", "code": "quota_exceeded", '
    '"resource": "storage", "plan": "free", "limit": 1, "used": 1}';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('a facet change does not notify the root', () {
    // One real operation per facet, each followed by the notify it makes (or
    // the one its caller makes).
    final changes = <String, Future<void> Function(ProjectNotifier n)>{
      'geo': (n) async {
        n.geoFacetWriter.replaceKeepingLod(_geo());
        n.notifyListeners();
      },
      'selection': (n) async => n.selectDay('2026-05-02'),
      'style': (n) async {
        n.styleFacetWriter.setLanguages(['de']);
        n.notifyListeners();
      },
      'items': (n) async => n.removeItemLocally(0),
      'elevation': (n) async {
        n.elevationFacetWriter.setTracks(_track, {'1': _track});
        n.notifyListeners();
      },
    };

    for (final MapEntry(key: facet, value: change) in changes.entries) {
      test(facet, () async {
        final n = _loaded();
        addTearDown(n.dispose);
        final heard = _record(n);

        await change(n);

        expect(heard, [facet],
            reason: "only the $facet facet's listeners hear of it");
      });
    }
  });

  group('a root change notifies the root', () {
    final changes = <String, void Function(ProjectNotifier n)>{
      'isLoading': (n) => n.isLoading = true,
      'error': (n) => n.error = 'Could not save',
      'loadErrorStatus': (n) => n.loadErrorStatus = 404,
      'offlineFromCache': (n) => n.offlineFromCache = true,
      'isMetaLoaded': (n) => n.isMetaLoaded = false,
      'isElevationLoaded': (n) => n.isElevationLoaded = false,
      'isSyncMetaLoaded': (n) => n.isSyncMetaLoaded = false,
      'ref': (n) => n.ref = const ProjectRef(name: 'Other'),
      'shareToken': (n) => n.shareToken = 't',
      'autoSyncEnabled': (n) => n.autoSyncEnabled = false,
      'pendingSync': (n) => n.pendingSync = (strava: [], polarsteps: []),
      'degradedRouteUpgradeAvailable': (n) =>
          n.degradedRouteUpgradeAvailable = true,
      'members': (n) => n.members = const [
            ProjectMember(
                userId: 1, displayName: 'A', avatarUrl: '', role: 'owner'),
          ],
      'memberInviteToken': (n) => n.memberInviteToken = 'inv',
      'quotaError': (n) => n.recordQuotaRefusal(402, _quotaBody),
      'polarstepsOverlaySteps': (n) => n.polarstepsOverlaySteps = [{}],
    };

    for (final MapEntry(key: field, value: change) in changes.entries) {
      test(field, () {
        final n = _loaded();
        addTearDown(n.dispose);
        final heard = _record(n);

        change(n);
        n.notifyListeners();

        expect(heard, ['root'],
            reason: 'a root write and a notify tell the root once, and no '
                'facet, none being written');
      });
    }
  });

  test('a facet-only operation, then a notify, reaches only that facet', () {
    final n = _loaded();
    addTearDown(n.dispose);
    final heard = _record(n);

    n.selectionFacetWriter.selectActivity('1');
    n.itemsFacetWriter.markChanged();
    n.notifyListeners();
    n.notifyListeners(); // nothing written since

    expect(heard, ['selection', 'items']);
  });

  test('a root and a facet written together: the facet, then the root', () {
    final n = _loaded();
    addTearDown(n.dispose);
    final heard = _record(n);

    n.styleFacetWriter.setLanguages(['fr']);
    n.error = 'Could not save';
    n.notifyListeners();

    expect(heard, ['style', 'root']);
  });

  test('after dispose a root write marks nothing and notifies nobody', () {
    final n = _loaded();
    final heard = _record(n);

    n.dispose();
    n.isLoading = true;
    n.notifyListeners();

    expect(heard, isEmpty);
  });

  // The #294 verification: selecting a day used to rebuild every widget that
  // listens to the root — the whole screen — and with it the map's polyline
  // geometry. Asserted through the perf spans, as in
  // map_panel_facet_scope_test.dart: `build_specs` wraps the geo-derived work
  // (the polylines) and `style_markers` the restyle.
  group('selecting a day rebuilds no polyline geometry', () {
    setUp(() {
      perfSpans
        ..reset()
        ..enabled = true;
    });
    tearDown(() {
      perfSpans
        ..reset()
        ..enabled = true;
    });

    int specs() => perfSpans.blockingSpans['build_specs']?.length ?? 0;
    int restyles() => perfSpans.blockingSpans['style_markers']?.length ?? 0;

    final panels = <String,
        Widget Function(ProjectNotifier n, AnimatedMapController c)>{
      'MapPanel': (n, c) => MapPanel(
            notifier: n,
            mapController: c,
            basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
            initialLat: 20.0,
            initialLng: 30.0,
            initialZoom: 3.0,
          ),
      'ManageMapPanel': (n, c) => ManageMapPanel(
            notifier: n,
            mapController: c,
            basemapUrl: 'https://example.invalid/{z}/{x}/{y}.png',
            fittedNotifier: ValueNotifier<bool>(true),
            initialLat: 20.0,
            initialLng: 30.0,
            initialZoom: 3.0,
          ),
    };

    for (final MapEntry(key: name, value: panel) in panels.entries) {
      testWidgets(name, (tester) async {
        tester.view.physicalSize = const Size(800, 800);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final c = AnimatedMapController(vsync: const TestVSync());
        addTearDown(c.dispose);
        final n = _loaded();
        addTearDown(n.dispose);
        var screenBuilds = 0;

        // The panel under a root listener, as the screens hold it.
        await tester.pumpWidget(MaterialApp(
          home: Scaffold(
            body: ListenableBuilder(
              listenable: n,
              builder: (_, __) {
                screenBuilds++;
                return panel(n, c);
              },
            ),
          ),
        ));
        await tester.pump();
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 800));
        final builds = screenBuilds;
        expect(specs(), 1, reason: 'the first build derives the specs once');
        final restyled = restyles();

        n.selectDay('2026-05-02');
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 800));

        expect(n.selectionFacet.selectedDay, '2026-05-02');
        expect(screenBuilds, builds,
            reason: 'the root did not notify: the screen is not rebuilt');
        expect(specs(), 1, reason: 'no polyline geometry is rebuilt');
        expect(restyles(), greaterThan(restyled),
            reason: 'the panel still restyles for the selected day');
      });
    }
  });
}
