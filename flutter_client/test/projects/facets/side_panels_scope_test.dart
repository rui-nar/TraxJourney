// The side panels listen to the facets they read, not to the whole notifier
// (issue #294, unit U17 of docs/CLIENT_STATE_MAP_PLAN.md): the elevation chart
// follows the elevation facet, the day carousel the items and the selection,
// the filter sheet the items and the selection, the activity panel's tiles
// the selection. Each harness here has no parent that rebuilds on the
// notifier, so whatever rebuilds does so on its own subscription.

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/perf_timing.dart' show perfSpans;
import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/day_carousel.dart';
import 'package:traxjourney_client/src/projects/elevation_chart.dart';
import 'package:traxjourney_client/src/projects/facets/project_facet.dart'
    show GeoLod;
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

int _builds(String span) => perfSpans.blockingSpans[span]?.length ?? 0;

// One activity with a 10 km elevation profile, a point every 100 m, so a tap
// anywhere across the chart lands on a spot.
Map<String, dynamic> _profiled(int id, String day) => {
      'id': id,
      'name': 'Activity $id',
      'type': 'Hike',
      'distance': 10000,
      'total_elevation_gain': 100,
      'start_date_local': '${day}T08:00:00',
      'elevation_profile': [
        for (var i = 0; i <= 100; i++) [i / 10, 100 + i],
      ],
    };

ProjectNotifier _notifierWith(List<Map<String, dynamic>> activities) {
  final n = ProjectNotifier(ProjectService());
  n.itemsFacetWriter.setActivities(activities);
  n.itemsFacetWriter.setItems([
    for (final a in activities)
      {'item_type': 'activity', 'activity_id': a['id']},
  ]);
  // Flush the writes now, so the first notify a test makes tells only of its
  // own change.
  n.notifyListeners();
  return n;
}

// A track that puts every distance at [at].
List<(double, GeoPoint)> _trackAt(GeoPoint at) =>
    [(0.0, at), (10.0, at)];

Map<String, dynamic> _geo(double lon) => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': 1},
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [lon, 48.0],
              [lon + 0.01, 48.01],
            ],
          },
        },
      ],
    };

void _bigView(WidgetTester tester) {
  tester.view.physicalSize = const Size(1200, 900);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

void main() {
  late bool perfWasEnabled;
  setUp(() {
    perfWasEnabled = perfSpans.enabled;
    perfSpans.enabled = true;
  });
  tearDown(() => perfSpans.enabled = perfWasEnabled);

  group('ElevationChart', () {
    Widget chart(ProjectNotifier n, void Function(GeoPoint?) onCursor) =>
        MaterialApp(
          home: Scaffold(
            body: Center(
              child: SizedBox(
                width: 800,
                child: ElevationChart(
                  activities: n.itemsFacet.activities,
                  elevation: n.elevationFacet,
                  onCursorChanged: onCursor,
                ),
              ),
            ),
          ),
        );

    testWidgets('rebuilds when the elevation track is rebuilt, and maps the '
        'cursor through the new track', (tester) async {
      _bigView(tester);
      final n = _notifierWith([_profiled(1, '2026-06-01')]);
      addTearDown(n.dispose);
      const oldPlace = (lat: 1.0, lon: 1.0);
      const newPlace = (lat: 2.0, lon: 2.0);
      n.elevationFacetWriter.setTracks(_trackAt(oldPlace), const {});
      n.notifyListeners();

      final cursor = <GeoPoint?>[];
      await tester.pumpWidget(chart(n, cursor.add));
      await tester.pumpAndSettle();
      expect(find.byType(LineChart), findsOneWidget);

      // A geometry-only change is not the chart's business.
      var before = _builds('elevation_chart_build');
      n.geoFacetWriter.replace(_geo(2.0), GeoLod.lowRes);
      n.notifyListeners();
      await tester.pump();
      expect(_builds('elevation_chart_build'), before);

      // The notifier rebuilds the tracks: the chart rebuilds on its own,
      // with no parent passing it a new track.
      before = _builds('elevation_chart_build');
      n.elevationFacetWriter.setTracks(_trackAt(newPlace), const {});
      n.notifyListeners();
      await tester.pump();
      expect(_builds('elevation_chart_build'), greaterThan(before));

      await tester.tap(find.byType(LineChart));
      await tester.pumpAndSettle();
      expect(cursor, isNotEmpty);
      expect(cursor.last, newPlace,
          reason: 'the cursor maps through the track on the facet now');
    });

    testWidgets('a notifier swap follows the new facet', (tester) async {
      _bigView(tester);
      final a = _notifierWith([_profiled(1, '2026-06-01')]);
      final b = _notifierWith([_profiled(1, '2026-06-01')]);
      addTearDown(b.dispose);
      await tester.pumpWidget(chart(a, (_) {}));
      await tester.pumpAndSettle();

      await tester.pumpWidget(chart(b, (_) {}));
      a.dispose(); // as an account change does, after the swap

      final before = _builds('elevation_chart_build');
      b.elevationFacetWriter
          .setTracks(_trackAt((lat: 3.0, lon: 3.0)), const {});
      b.notifyListeners();
      await tester.pump();
      expect(_builds('elevation_chart_build'), greaterThan(before));
    });
  });

  group('DayCarousel', () {
    Widget carousel(ProjectNotifier n) => MaterialApp(
          home: Scaffold(
              body: SizedBox.expand(child: DayCarousel(notifier: n))),
        );

    // The centered card's label widget: a tile that rebuilt holds a new one.
    Text dayLabel(WidgetTester tester) =>
        tester.widget<Text>(find.text('Day 1'));

    testWidgets('a tile does not rebuild on a geometry-only change, and does '
        'on an items change', (tester) async {
      _bigView(tester);
      final n = _notifierWith(
          [_profiled(1, '2026-06-01'), _profiled(2, '2026-06-02')]);
      addTearDown(n.dispose);
      await tester.pumpWidget(carousel(n));
      await tester.pump();
      final label = dayLabel(tester);
      await tester.pump();
      expect(dayLabel(tester), same(label), reason: 'the harness is at rest');

      n.geoFacetWriter.replace(_geo(2.0), GeoLod.lowRes);
      n.notifyListeners();
      await tester.pump();
      expect(dayLabel(tester), same(label));

      n.itemsFacetWriter.setActivities([
        _profiled(1, '2026-06-01'),
        _profiled(2, '2026-06-02'),
        _profiled(3, '2026-06-03'),
      ]);
      n.notifyListeners();
      await tester.pump();
      expect(dayLabel(tester), isNot(same(label)));
      expect(find.text('3'), findsOneWidget, reason: 'the new day is listed');

      // Let the idle-retract timer fire before the test ends.
      await tester.pump(const Duration(seconds: 4));
    });

    testWidgets('a notifier swap follows the new facets', (tester) async {
      _bigView(tester);
      final a = _notifierWith([_profiled(1, '2026-06-01')]);
      final b = _notifierWith([_profiled(1, '2026-06-01')]);
      addTearDown(b.dispose);
      await tester.pumpWidget(carousel(a));
      await tester.pumpWidget(carousel(b));
      a.dispose();

      b.itemsFacetWriter.setActivities(
          [_profiled(1, '2026-06-01'), _profiled(2, '2026-06-02')]);
      b.notifyListeners();
      await tester.pump();
      expect(find.text('2'), findsOneWidget);

      await tester.pump(const Duration(seconds: 4));
    });
  });

  group('FilterSheet', () {
    testWidgets('updates when the first activity is added to an empty trip, '
        'and when a filter is toggled', (tester) async {
      _bigView(tester);
      final n = ProjectNotifier(ProjectService());
      addTearDown(n.dispose);
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(body: FilterSheet(notifier: n, readOnly: false)),
      ));
      expect(find.text('Activity type'), findsNothing);

      // Items: the trip now holds something to filter on.
      n.itemsFacetWriter.setActivities([_profiled(1, '2026-06-01')]);
      n.notifyListeners();
      await tester.pump();
      final hike = find.widgetWithText(FilterChip, 'Hike');
      expect(hike, findsOneWidget);
      expect(tester.widget<FilterChip>(hike).selected, isFalse);

      // Selection: the filter is ticked from elsewhere (the map's filter
      // button, a restored session) while the sheet is open.
      n.setFilters(activityTypes: {'hike'});
      await tester.pump();
      expect(tester.widget<FilterChip>(hike).selected, isTrue);
    });
  });

  group('ActivityPanel', () {
    Widget panel(ProjectNotifier n) => MaterialApp(
          home: Scaffold(body: ActivityPanel(notifier: n)),
        );

    Color? tileColor(WidgetTester tester) => tester
        .widget<ListTile>(find.ancestor(
            of: find.byIcon(Icons.hiking).first,
            matching: find.byType(ListTile)))
        .tileColor;

    Future<void> expandDays(WidgetTester tester) async {
      await tester.tap(find.byIcon(Icons.unfold_more));
      await tester.pumpAndSettle();
    }

    testWidgets('selecting an activity flips its tile without rebuilding '
        'the panel', (tester) async {
      _bigView(tester);
      final n = _notifierWith([_profiled(1, '2026-06-01')]);
      addTearDown(n.dispose);
      await tester.pumpWidget(panel(n));
      await expandDays(tester);
      expect(tileColor(tester), isNull);

      final before = _builds('activity_panel_build');
      n.selectActivity(1);
      await tester.pumpAndSettle();
      expect(tileColor(tester), isNotNull);
      expect(_builds('activity_panel_build'), before);
    });

    testWidgets('a notifier swap follows the new facets', (tester) async {
      _bigView(tester);
      final a = _notifierWith([_profiled(1, '2026-06-01')]);
      final b = _notifierWith([_profiled(1, '2026-06-01')]);
      addTearDown(b.dispose);
      await tester.pumpWidget(panel(a));
      await expandDays(tester);
      await tester.pumpWidget(panel(b));
      await tester.pumpAndSettle();
      a.dispose();

      b.selectActivity(1);
      await tester.pumpAndSettle();
      expect(tileColor(tester), isNotNull,
          reason: "the tile listens to the new notifier's selection");
    });
  });
}
