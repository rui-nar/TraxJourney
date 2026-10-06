// Issue #294 (Decision 9 of docs/CLIENT_STATE_MAP_PLAN.md) — both map panels
// key their caches on facet versions, not on the identity of what they read.
//
// The geometry specs are keyed on the geometry and style versions, the item
// list's own version and whether journals show; the restyle on the selection
// version. So a notify that changes none of those — root-only state, a
// day-note save, an elevation merge — does no spec work, a selection change
// only restyles, and a swapped notifier (an account change, whose new facets
// restart their versions) rebuilds. Asserted through the perf spans, as in
// map_panel_selection_cost_test.dart: `build_specs` wraps the geo-derived
// work and `style_markers` the restyle.

import 'package:flutter/material.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_filters.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

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

ProjectNotifier _notifier() => ProjectNotifier(ProjectService())
  ..ref = const ProjectRef(name: 'Trip')
  ..geoFacetWriter.replaceKeepingLod(_geo())
  ..itemsFacetWriter.setActivities([
    {'id': '1', 'start_date_local': '2026-05-01T08:00:00'},
    {'id': '2', 'start_date_local': '2026-05-02T08:00:00'},
  ])
  ..itemsFacetWriter.setItems([
    {'item_type': 'activity', 'activity_id': '1'},
    {'item_type': 'activity', 'activity_id': '2'},
  ])
  ..isLoading = false;

const _basemap = 'https://example.invalid/{z}/{x}/{y}.png';

typedef _Panel = Widget Function(
    ProjectNotifier n, AnimatedMapController c, bool autoZoom);

Widget _viewPanel(ProjectNotifier n, AnimatedMapController c, bool autoZoom) =>
    MapPanel(
      notifier: n,
      mapController: c,
      basemapUrl: _basemap,
      autoZoom: autoZoom,
      initialLat: 20.0,
      initialLng: 30.0,
      initialZoom: 3.0,
    );

Widget _managePanel(
        ProjectNotifier n, AnimatedMapController c, bool autoZoom) =>
    ManageMapPanel(
      notifier: n,
      mapController: c,
      basemapUrl: _basemap,
      autoZoom: autoZoom,
      fittedNotifier: ValueNotifier<bool>(true),
      initialLat: 20.0,
      initialLng: 30.0,
      initialZoom: 3.0,
    );

/// The panel rebuilt by a root listener above it, as the screens do today.
Widget _underRoot(_Panel panel, ProjectNotifier n, AnimatedMapController c,
        {bool autoZoom = false}) =>
    MaterialApp(
      home: Scaffold(
        body: ListenableBuilder(
          listenable: n,
          builder: (_, __) => panel(n, c, autoZoom),
        ),
      ),
    );

Future<void> _settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 800));
}

void _bigView(WidgetTester tester) {
  tester.view.physicalSize = const Size(800, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

int _specs() => perfSpans.blockingSpans['build_specs']?.length ?? 0;
int _restyles() => perfSpans.blockingSpans['style_markers']?.length ?? 0;

void _suite(String name, _Panel panel) {
  group(name, () {
    late AnimatedMapController c;

    Future<ProjectNotifier> pumpPanel(WidgetTester tester,
        {bool autoZoom = false}) async {
      _bigView(tester);
      c = AnimatedMapController(vsync: const TestVSync());
      addTearDown(c.dispose);
      final n = _notifier();
      await tester.pumpWidget(_underRoot(panel, n, c, autoZoom: autoZoom));
      await _settle(tester);
      expect(_specs(), 1, reason: 'the first build derives the specs once');
      expect(_restyles(), 1);
      return n;
    }

    testWidgets('a root-only notify does no spec work', (tester) async {
      final n = await pumpPanel(tester);

      n.isLoading = true;
      n.notifyListeners();
      await tester.pump();
      n.isLoading = false;
      n.notifyListeners();
      await tester.pump();

      expect(_specs(), 1);
      expect(_restyles(), 1);
    });

    testWidgets('a day-note save does no spec work', (tester) async {
      final n = await pumpPanel(tester);

      n.itemsFacetWriter.setDayMeta({
        '2026-05-01': {'note': 'Rain all day'},
      });
      n.notifyListeners();
      await tester.pump();

      expect(_specs(), 1);
      expect(_restyles(), 1);
    });

    testWidgets('an elevation merge does no spec work', (tester) async {
      final n = await pumpPanel(tester);

      // As the elevation upgrade does: new activity data and new tracks, the
      // item list untouched (Decision 23).
      n.itemsFacetWriter.setActivities([
        {
          'id': '1',
          'start_date_local': '2026-05-01T08:00:00',
          'total_elevation_gain': 820,
        },
        {'id': '2', 'start_date_local': '2026-05-02T08:00:00'},
      ]);
      const List<(double, GeoPoint)> track = [
        (0.0, (lat: 47.25, lon: 11.35)),
        (1.0, (lat: 47.35, lon: 11.55)),
      ];
      n.elevationFacetWriter.setTracks(track, {'1': track});
      n.notifyListeners();
      await tester.pump();

      expect(_specs(), 1);
      expect(_restyles(), 1);
    });

    testWidgets('a selection change only restyles', (tester) async {
      final n = await pumpPanel(tester);

      n.selectionFacetWriter.setSelectedDays({'2026-05-02'});
      n.notifyListeners();
      await tester.pump();
      n.selectActivity('1');
      await tester.pump();

      expect(_specs(), 1);
      expect(_restyles(), 3);
    });

    testWidgets('a style change rebuilds the specs, the secondary colour too',
        (tester) async {
      final n = await pumpPanel(tester);

      // The secondary track colour was never part of the old style guard.
      n.styleFacetWriter.setTrackStyle(secondaryColor: const Color(0xFF00FF00));
      n.notifyListeners();
      await tester.pump();

      expect(_specs(), 2);
    });

    testWidgets('the panel listens to the facets itself', (tester) async {
      _bigView(tester);
      c = AnimatedMapController(vsync: const TestVSync());
      addTearDown(c.dispose);
      final n = _notifier();
      // No root listener above the panel: only its own facet subscription can
      // rebuild it.
      await tester.pumpWidget(MaterialApp(home: Scaffold(body: panel(n, c, false))));
      await _settle(tester);
      expect(_specs(), 1);

      n.selectionFacetWriter.setSelectedDays({'2026-05-01'});
      n.notifyListeners();
      await tester.pump();
      expect(_restyles(), 2);

      n.geoFacetWriter.replaceKeepingLod(_geo());
      n.notifyListeners();
      await tester.pump();
      expect(_specs(), 2);
    });

    testWidgets('a swapped notifier rebuilds, and the old one is not heard',
        (tester) async {
      _bigView(tester);
      c = AnimatedMapController(vsync: const TestVSync());
      addTearDown(c.dispose);
      // Built the same way, so their facets hold the same versions: only the
      // facets' identity tells them apart.
      final first = _notifier();
      final second = _notifier();
      expect(second.geoFacet.version, first.geoFacet.version);
      expect(second.styleFacet.version, first.styleFacet.version);
      expect(second.itemsFacet.listVersion, first.itemsFacet.listVersion);
      expect(second.selectionFacet.version, first.selectionFacet.version);
      final current = ValueNotifier(first);
      addTearDown(current.dispose);

      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ValueListenableBuilder<ProjectNotifier>(
            valueListenable: current,
            builder: (_, n, __) => panel(n, c, false),
          ),
        ),
      ));
      await _settle(tester);
      expect(_specs(), 1);
      expect(_restyles(), 1);

      current.value = second;
      await tester.pump();
      expect(_specs(), 2, reason: 'a new notifier\'s facets are new state');
      expect(_restyles(), 2);

      // The panel now follows the new notifier's facets, not the old one's.
      first.selectionFacetWriter.setSelectedDays({'2026-05-01'});
      first.notifyListeners();
      await tester.pump();
      expect(_restyles(), 2);

      second.selectionFacetWriter.setSelectedDays({'2026-05-01'});
      second.notifyListeners();
      await tester.pump();
      expect(_restyles(), 3);
      // MapPanel refits on a new notifier; let that animation finish.
      await _settle(tester);
    });

    testWidgets(
        'a selection write that does not move the selection does not refit',
        (tester) async {
      final n = await pumpPanel(tester, autoZoom: true);

      n.selectionFacetWriter.setSelectedDays({'2026-05-02'});
      n.notifyListeners();
      await _settle(tester);
      // Auto-zoom fitted the selected day: Lisbon.
      expect(c.mapController.camera.center.latitude, closeTo(38.725, 0.2));

      // Move away, then re-apply the same filters: the facet is written but
      // the selection points at the same day.
      c.mapController.move(c.mapController.camera.center, 4.0);
      await _settle(tester);
      final restyles = _restyles();
      n.selectionFacetWriter.setFilters(ProjectFilters.empty, {'2026-05-02'});
      n.notifyListeners();
      await _settle(tester);

      expect(_restyles(), restyles + 1, reason: 'any selection write restyles');
      expect(c.mapController.camera.zoom, closeTo(4.0, 0.001),
          reason: 'but only a moved selection refits');
    });
  });
}

void main() {
  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    perfSpans
      ..reset()
      ..enabled = true;
  });

  tearDown(() {
    perfSpans
      ..reset()
      ..enabled = true; // the library default — see PerfSpans.enabled
  });

  _suite('MapPanel', _viewPanel);
  _suite('ManageMapPanel', _managePanel);
}
