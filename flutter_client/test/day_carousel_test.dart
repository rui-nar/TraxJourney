import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/projects/day_carousel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Issue #199: view-mode vertical day carousel. Covers the pure
/// index→day-key selection mapping, the scroll-distance→scale/offset
/// mapping, the "Day N" label (shown on the highlighted/centered card in
/// every layout) + distance/climb stats (highlighted + non-compact only)
/// (sourced from the same `dayTripNumbering`/`dayStats` the manage-mode day
/// hero and map selection overlay already use), the responsive compact
/// breakpoint, the pill shape, the staggered leftward bulge, and the idle
/// dim/retract.
void main() {
  ProjectNotifier notifierWithThreeDays() {
    final n = ProjectNotifier(ProjectService());
    n.activities = [
      {
        'id': 1,
        'distance': 10000, // 10 km
        'total_elevation_gain': 250,
        'start_date_local': '2026-06-01T08:00:00',
      },
      {
        'id': 2,
        'distance': 5000, // 5 km
        'total_elevation_gain': 100,
        'start_date_local': '2026-06-03T08:00:00',
      },
    ];
    n.items = [
      {'item_type': 'activity', 'activity_id': 1},
      {
        'item_type': 'memory',
        'memory': {'id': 1, 'date': '2026-06-02'},
      },
      {'item_type': 'activity', 'activity_id': 2},
    ];
    return n;
  }

  Future<ProjectNotifier> pumpCarousel(WidgetTester tester, Size size) async {
    tester.view.physicalSize = size;
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final notifier = notifierWithThreeDays();
    await tester.pumpWidget(
      ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: MaterialApp(
          // SizedBox.expand mirrors the tight height DayCarousel gets in
          // production from the Positioned(top:0, bottom:0) overlay in
          // view_screen.dart — ListWheelScrollView needs a bounded height.
          home: Scaffold(
              body: SizedBox.expand(child: DayCarousel(notifier: notifier))),
        ),
      ),
    );
    await tester.pump();
    return notifier;
  }

  group('dayCarouselSelection', () {
    test('maps a valid index to the day key at that position', () {
      final days = ['2026-06-01', '2026-06-02', '2026-06-03'];
      expect(dayCarouselSelection(0, days), '2026-06-01');
      expect(dayCarouselSelection(2, days), '2026-06-03');
    });

    test('returns null for an out-of-range index', () {
      final days = ['2026-06-01', '2026-06-02'];
      expect(dayCarouselSelection(-1, days), isNull);
      expect(dayCarouselSelection(2, days), isNull);
    });
  });

  group('dayCarouselScale', () {
    test('is the full magnification at the centered slot', () {
      expect(dayCarouselScale(0, 2.0), 2.0);
    });

    test('falls back to 1x a full slot or more away', () {
      expect(dayCarouselScale(1.0, 2.0), 1.0);
      expect(dayCarouselScale(3.0, 2.0), 1.0);
    });

    test('interpolates linearly in between', () {
      expect(dayCarouselScale(0.5, 2.0), closeTo(1.5, 1e-9));
    });
  });

  group('dayCarouselOffset', () {
    test('is the full offset at the centered slot', () {
      expect(dayCarouselOffset(0, 26.0), 26.0);
    });

    test('falls back to 0 a full slot or more away', () {
      expect(dayCarouselOffset(1.0, 26.0), 0.0);
      expect(dayCarouselOffset(3.0, 26.0), 0.0);
    });

    test('interpolates linearly in between', () {
      expect(dayCarouselOffset(0.5, 26.0), closeTo(13.0, 1e-9));
    });
  });

  testWidgets(
      'wide layout: highlighted day shows "Day N" + stats, others show a bare number',
      (tester) async {
    await pumpCarousel(tester, const Size(1200, 900));

    // Day 1 starts centered/highlighted (nothing pre-selected).
    expect(find.text('Day 1'), findsOneWidget);
    expect(find.text('10 km'), findsOneWidget);
    expect(find.text('250 m'), findsOneWidget);

    // Neighbors show the bare number, no "Day" prefix or stats.
    expect(find.text('2'), findsOneWidget);
    expect(find.text('3'), findsOneWidget);
    expect(find.text('Day 2'), findsNothing);
    expect(find.text('0 km'), findsNothing);
  });

  testWidgets(
      'narrow layout: compact strip still shows "Day N" when highlighted, but never stats',
      (tester) async {
    await pumpCarousel(tester, const Size(360, 800));

    // The leftward bulge gives the highlighted card room for "Day N" even
    // in the compact strip (issue #199 feedback).
    expect(find.text('Day 1'), findsOneWidget);
    expect(find.text('1'), findsNothing);
    expect(find.text('10 km'), findsNothing);
  });

  testWidgets(
      "the centered card bulges left past the pill's edge; neighbors stay flush",
      (tester) async {
    await pumpCarousel(tester, const Size(1200, 900));

    // One Transform.translate per rendered day card (3 days in the fixture).
    // Day 1 (index 0) starts centered — offset the full maxOffset (42px,
    // the DayCarousel default) left; day 3 (two slots away) sits flush
    // against the pill (0px). Issue #199 feedback: staggered/offset stack.
    final dxs = tester
        .widgetList<Transform>(find.byType(Transform))
        .map((t) => t.transform.getTranslation().x)
        .toList()
      ..sort();
    expect(dxs.first, closeTo(-42.0, 0.5));
    expect(dxs.last, closeTo(0.0, 0.5));
  });

  testWidgets('dragging the carousel updates the notifier\'s selected day',
      (tester) async {
    final notifier = await pumpCarousel(tester, const Size(1200, 900));
    expect(notifier.selectedDays, isEmpty);

    // Large drag guarantees landing on the last day regardless of the
    // wheel's residual curvature.
    await tester.drag(find.byType(ListWheelScrollView), const Offset(0, -1000));
    await tester.pumpAndSettle();

    expect(notifier.selectedDays, {'2026-06-03'});
  });

  testWidgets('carousel is a pill: only the right edge is rounded, radius = width / 2',
      (tester) async {
    await pumpCarousel(tester, const Size(1200, 900));

    final container = tester.widget<Container>(find.descendant(
      of: find.byType(DayCarousel),
      matching: find.byWidgetPredicate(
          (w) => w is Container && w.decoration is BoxDecoration),
    ));
    final radius =
        (container.decoration as BoxDecoration).borderRadius as BorderRadius;
    // width is 120 in the wide layout — radius = width / 2 for a true pill cap.
    expect(radius.topRight, const Radius.circular(60));
    expect(radius.bottomRight, const Radius.circular(60));
    expect(radius.topLeft, Radius.zero);
    expect(radius.bottomLeft, Radius.zero);
  });

  testWidgets('idle-dims and retracts, then reveals again on tap',
      (tester) async {
    await pumpCarousel(tester, const Size(1200, 900));

    // Freshly mounted: fully revealed.
    expect(
      tester.widget<AnimatedOpacity>(find.byType(AnimatedOpacity)).opacity,
      1.0,
    );

    // Cross the idle delay (fires the retract Timer + its setState), then
    // let the resulting opacity/slide animation finish — two separate pumps,
    // since the animation's own ticker only starts once the Timer callback
    // above has run.
    await tester.pump(const Duration(seconds: 3, milliseconds: 50));
    await tester.pump(const Duration(milliseconds: 300));
    expect(
      tester.widget<AnimatedOpacity>(find.byType(AnimatedOpacity)).opacity,
      lessThan(1.0),
    );
    expect(
      tester.widget<AnimatedSlide>(find.byType(AnimatedSlide)).offset.dx,
      greaterThan(0),
    );

    // Tapping the dimmed strip reveals it again.
    await tester.tap(find.byType(DayCarousel));
    await tester.pump();
    expect(
      tester.widget<AnimatedOpacity>(find.byType(AnimatedOpacity)).opacity,
      1.0,
    );
  });

  testWidgets('renders nothing when the project has no days', (tester) async {
    final notifier = ProjectNotifier(ProjectService());
    await tester.pumpWidget(
      ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: MaterialApp(
          home: Scaffold(body: DayCarousel(notifier: notifier)),
        ),
      ),
    );

    expect(find.byType(ListWheelScrollView), findsNothing);
  });

  // Issue #322: the map is the other half of this control. Tapping an
  // activity there calls selectActivity(), and the strip has to follow it.
  group('following a map selection', () {
    testWidgets('scrolls the strip to the tapped activity\'s day',
        (tester) async {
      final notifier = await pumpCarousel(tester, const Size(1200, 900));
      expect(find.text('Day 1'), findsOneWidget);

      // Activity 2 is on 2026-06-03 — the trip's third day.
      notifier.selectActivity(2);
      await tester.pumpAndSettle();

      expect(find.text('Day 3'), findsOneWidget);
      expect(find.text('5 km'), findsOneWidget);
      expect(find.text('Day 1'), findsNothing);
    });

    testWidgets('does not turn the map selection into a day selection',
        (tester) async {
      final notifier = await pumpCarousel(tester, const Size(1200, 900));
      notifier.selectActivity(2);
      await tester.pumpAndSettle();

      // The wheel moved for a selection it did not make, so it must not
      // commit one of its own: selectDays() clears selectedActivityId, which
      // would undo the tap and bounce the wheel back — the feedback loop
      // this guard exists to prevent.
      expect(notifier.selectedActivityId, 2);
      expect(notifier.selectedDays, isEmpty);
      expect(notifier.selectedDay, isNull);
    });

    testWidgets('a second tap mid-scroll still leaves the map selection alone',
        (tester) async {
      final notifier = await pumpCarousel(tester, const Size(1200, 900));

      notifier.selectActivity(2); // day 3
      await tester.pump(const Duration(milliseconds: 100)); // mid-animation
      notifier.selectActivity(1); // day 1, before the first scroll lands
      await tester.pumpAndSettle();

      // The superseded animation completes as soon as the second one starts;
      // if that were allowed to lower the loop guard, the rest of the second
      // scroll would commit a day and clear the activity under it.
      expect(notifier.selectedActivityId, 1);
      expect(notifier.selectedDays, isEmpty);
      expect(find.text('Day 1'), findsOneWidget);
    });

    testWidgets('leaves the strip alone for an activity on no day',
        (tester) async {
      final notifier = await pumpCarousel(tester, const Size(1200, 900));

      // Activity 99 is in no item of the trip, so it belongs to no day.
      notifier.selectActivity(99);
      await tester.pumpAndSettle();

      expect(find.text('Day 1'), findsOneWidget);
      expect(notifier.selectedDays, isEmpty);
    });

    testWidgets('leaves the strip where it is when the map deselects',
        (tester) async {
      final notifier = await pumpCarousel(tester, const Size(1200, 900));
      notifier.selectActivity(2);
      await tester.pumpAndSettle();

      // A second tap on the same activity deselects it.
      notifier.selectActivity(2);
      await tester.pumpAndSettle();

      expect(notifier.selectedActivityId, isNull);
      expect(find.text('Day 3'), findsOneWidget);
    });

    testWidgets('a map selection is harmless when the strip has no days',
        (tester) async {
      final notifier = ProjectNotifier(ProjectService());
      await tester.pumpWidget(
        ChangeNotifierProvider<ProjectNotifier>.value(
          value: notifier,
          child: MaterialApp(
            home: Scaffold(body: DayCarousel(notifier: notifier)),
          ),
        ),
      );

      // Nothing rendered means the wheel controller has no clients — the
      // follow must no-op rather than throw.
      notifier.selectActivity(1);
      await tester.pumpAndSettle();

      expect(find.byType(ListWheelScrollView), findsNothing);
      expect(notifier.selectedActivityId, 1);
    });
  });
}
