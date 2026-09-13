import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/core/scrolling_selectable_text.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Regression coverage for wiring the activity panel's selection state into
/// [ScrollingSelectableText.isSelected] for each of the four row kinds
/// (activity, day, memory, encounter). The marquee/tooltip mechanics
/// themselves (given isSelected + overflow) are covered exhaustively in
/// scrolling_selectable_text_test.dart; this file only asserts that each row
/// kind computes and threads its own `isSelected` correctly, and that
/// selecting one kind doesn't leak into another.
void main() {
  const longActivityName = 'A remarkably long activity name that overflows';
  const longMemoryLabel = 'A remarkably long memory label that overflows';
  const longPersonName = 'A remarkably long person name that overflows';

  ProjectNotifier buildNotifier() {
    final n = ProjectNotifier(ProjectService());
    n.people = [
      {'id': 1, 'name': longPersonName},
    ];
    n.activities = [
      {
        'id': 1,
        'type': 'Run',
        'name': longActivityName,
        'distance': 5000,
        'moving_time': 1800,
        'start_date_local': '2026-06-01T08:00:00',
      },
    ];
    n.items = [
      {'item_type': 'activity', 'activity_id': 1},
      {
        'item_type': 'memory',
        'memory': {'id': 'm1', 'name': longMemoryLabel, 'date': '2026-06-01'},
      },
      {
        'item_type': 'encounter',
        'encounter': {'id': 'e1', 'person_id': 1, 'date': '2026-06-01'},
      },
    ];
    return n;
  }

  Future<ProjectNotifier> pumpPanel(WidgetTester tester) async {
    final notifier = buildNotifier();
    await tester.pumpWidget(
      ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: MaterialApp(
          home: Scaffold(body: ActivityPanel(notifier: notifier)),
        ),
      ),
    );
    // Days start collapsed; expand so the rows render.
    await tester.tap(find.byIcon(Icons.unfold_more));
    await tester.pumpAndSettle();
    return notifier;
  }

  bool isSelectedFor(WidgetTester tester, String text) => tester
      .widget<ScrollingSelectableText>(find.byWidgetPredicate(
          (w) => w is ScrollingSelectableText && w.text == text))
      .isSelected;

  bool isSelectedForDay(WidgetTester tester) => tester
      .widget<ScrollingSelectableText>(find.byWidgetPredicate((w) =>
          w is ScrollingSelectableText && w.text.startsWith('Day 1')))
      .isSelected;

  testWidgets('nothing is selected initially', (tester) async {
    await pumpPanel(tester);

    expect(isSelectedFor(tester, longActivityName), isFalse);
    expect(isSelectedFor(tester, longMemoryLabel), isFalse);
    expect(isSelectedFor(tester, 'Met $longPersonName'), isFalse);
    expect(isSelectedForDay(tester), isFalse);
  });

  testWidgets('tapping the activity selects only the activity', (tester) async {
    await pumpPanel(tester);

    await tester.tap(find.text(longActivityName));
    await tester.pumpAndSettle();

    expect(isSelectedFor(tester, longActivityName), isTrue);
    expect(isSelectedFor(tester, longMemoryLabel), isFalse);
    expect(isSelectedFor(tester, 'Met $longPersonName'), isFalse);
    expect(isSelectedForDay(tester), isFalse);

    // Tapping again deselects (selectActivity toggles).
    await tester.tap(find.text(longActivityName));
    await tester.pumpAndSettle();
    expect(isSelectedFor(tester, longActivityName), isFalse);
  });

  testWidgets('tapping the memory selects only the memory', (tester) async {
    final notifier = await pumpPanel(tester);

    // Tapping a memory row also opens its detail modal (existing behavior,
    // unrelated to this feature) which has a pre-existing layout overflow
    // at the default test viewport size, so drive selection directly here
    // rather than via tester.tap to keep this test scoped to the wiring
    // this feature actually touches.
    notifier.selectMemory('m1');
    await tester.pumpAndSettle();

    expect(isSelectedFor(tester, longMemoryLabel), isTrue);
    expect(isSelectedFor(tester, longActivityName), isFalse);
    expect(isSelectedFor(tester, 'Met $longPersonName'), isFalse);
    expect(isSelectedForDay(tester), isFalse);
  });

  testWidgets('tapping the encounter selects only that encounter',
      (tester) async {
    await pumpPanel(tester);

    await tester.tap(find.text('Met $longPersonName'));
    await tester.pump(); // opens a detail sheet; one frame is enough to see
    // the local selection flag flip.

    expect(isSelectedFor(tester, 'Met $longPersonName'), isTrue);
    expect(isSelectedFor(tester, longActivityName), isFalse);
    expect(isSelectedFor(tester, longMemoryLabel), isFalse);
    expect(isSelectedForDay(tester), isFalse);
  });

  testWidgets('tapping the day header highlights only the day', (tester) async {
    await pumpPanel(tester);

    await tester.tap(find.byWidgetPredicate((w) =>
        w is ScrollingSelectableText && w.text.startsWith('Day 1')));
    await tester.pumpAndSettle();

    expect(isSelectedForDay(tester), isTrue);
    expect(isSelectedFor(tester, longActivityName), isFalse);
    expect(isSelectedFor(tester, longMemoryLabel), isFalse);
    expect(isSelectedFor(tester, 'Met $longPersonName'), isFalse);
  });
}
