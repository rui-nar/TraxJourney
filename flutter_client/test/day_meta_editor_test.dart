import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';

import 'package:traxjourney_client/src/core/design_tokens.dart';
import 'package:traxjourney_client/src/projects/day_meta_editor.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart' show dayTripNumbering;

/// Pumps a bare [DayMetaEditor] and returns the meta captured by the most
/// recent onSave. Edits make the editor dirty, which enables "Save day".
class _Harness {
  Map<String, dynamic>? saved;
}

Future<_Harness> _pump(
  WidgetTester tester, {
  Map<String, dynamic> initialMeta = const {},
  List<String> sleepingOptions = const [],
  Map<String, String> sleepingOptionGroups = const {},
  List<String> availableTags = const [],
  List<Map<String, dynamic>> counters = const [],
  bool countersOnly = false,
  int dayNumber = 21,
  int totalDays = 47,
  List<String> effectiveTags = const [],
  double distanceKm = 0,
  double elevationM = 0,
}) async {
  final h = _Harness();
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: SafeArea(
        child: DayMetaEditor(
          dateKey: '2026-06-14',
          initialMeta: initialMeta,
          sleepingOptions: sleepingOptions,
          sleepingOptionGroups: sleepingOptionGroups,
          availableTags: availableTags,
          counters: counters,
          countersOnly: countersOnly,
          dayNumber: dayNumber,
          totalDays: totalDays,
          date: DateTime(2026, 6, 14),
          effectiveTags: effectiveTags,
          distanceKm: distanceKm,
          elevationM: elevationM,
          onSave: (m) => h.saved = m,
        ),
      ),
    ),
  ));
  return h;
}

bool _saveEnabled(WidgetTester tester) {
  final btn = tester.widget<TextButton>(
    find.ancestor(of: find.text('Save day'), matching: find.byType(TextButton)),
  );
  return btn.onPressed != null;
}

Future<void> _tapSave(WidgetTester tester) async {
  await tester.tap(find.text('Save day'));
  await tester.pump();
}

/// Scrolls a body control into view (the modal body is scrollable) then taps it.
Future<void> _tapVisible(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.pumpAndSettle();
  await tester.tap(finder);
  await tester.pump();
}

void main() {
  // Avoid network font fetches during tests — fall back to a bundled font.
  setUpAll(() => GoogleFonts.config.allowRuntimeFetching = false);

  group('dayTripNumbering', () {
    test('contiguous days number by calendar position', () {
      final keys = ['2026-06-01', '2026-06-02', '2026-06-03'];
      expect(dayTripNumbering('2026-06-02', keys, null),
          (dayNumber: 2, totalDays: 3));
    });

    test('gaps are counted (matches the panel headers)', () {
      final keys = ['2026-06-01', '2026-06-03', '2026-06-06']; // missing days
      expect(dayTripNumbering('2026-06-03', keys, null).dayNumber, 3);
      expect(dayTripNumbering('2026-06-06', keys, null),
          (dayNumber: 6, totalDays: 6));
    });

    test('result is independent of list sort order', () {
      final descending = ['2026-06-06', '2026-06-03', '2026-06-01'];
      expect(dayTripNumbering('2026-06-03', descending, null),
          (dayNumber: 3, totalDays: 6));
    });

    test('an explicit earlier trip start shifts the numbering', () {
      final keys = ['2026-06-03', '2026-06-04'];
      expect(dayTripNumbering('2026-06-03', keys, '2026-06-01'),
          (dayNumber: 3, totalDays: 4));
    });
  });

  group('Edit Day hero', () {
    testWidgets('shows day number, progress and dist/climb', (tester) async {
      await _pump(tester,
          dayNumber: 21, totalDays: 47, distanceKm: 88, elevationM: 276);

      expect(find.text('21'), findsOneWidget); // big mono day number
      expect(find.text('21 / 47'), findsOneWidget); // progress label
      expect(find.text('DIST'), findsOneWidget);
      expect(find.text('88'), findsOneWidget);
      expect(find.text('CLIMB'), findsOneWidget);
      expect(find.text('276'), findsOneWidget);
    });

    testWidgets('hides the stat strip when the day has no activity', (tester) async {
      await _pump(tester, distanceKm: 0, elevationM: 0);
      expect(find.text('DIST'), findsNothing);
      expect(find.text('CLIMB'), findsNothing);
    });

    testWidgets('renders effective tags as the hero context', (tester) async {
      await _pump(tester, effectiveTags: ['EV10 — North Cape']);
      // Appears twice: once in the hero, once as the pre-selected chip in
      // the Tags section below (see "pre-selects the inherited tag" test).
      expect(find.text('EV10 — North Cape'), findsNWidgets(2));
    });
  });

  group('Chip groups', () {
    testWidgets('selecting difficulty puts the value in saved meta', (tester) async {
      final h = await _pump(tester);
      expect(_saveEnabled(tester), isFalse); // clean → disabled

      await tester.tap(find.text('Hard'));
      await tester.pump();
      expect(_saveEnabled(tester), isTrue);

      await _tapSave(tester);
      expect(h.saved!['difficulty'], 'hard');
    });

    testWidgets('the dashed "Not set" chip clears a value', (tester) async {
      final h = await _pump(tester, initialMeta: {'difficulty': 'hard'});
      // First "Not set" belongs to the Difficulty group (first section).
      await tester.tap(find.text('Not set').first);
      await tester.pump();
      await _tapSave(tester);
      expect(h.saved!.containsKey('difficulty'), isFalse);
    });

    testWidgets('sleeping chips carry their category dot colour', (tester) async {
      await _pump(tester,
          sleepingOptions: ['Hotel', 'Camping'],
          sleepingOptionGroups: {'Hotel': 'Indoors', 'Camping': 'Outdoors'});

      Finder dot(Color c) => find.byWidgetPredicate((w) =>
          w is Container &&
          w.decoration is BoxDecoration &&
          (w.decoration as BoxDecoration).color == c &&
          (w.decoration as BoxDecoration).shape == BoxShape.circle);

      expect(dot(kSleepIndoors), findsOneWidget); // Hotel
      expect(dot(kSleepOutdoors), findsOneWidget); // Camping
    });
  });

  group('Tags', () {
    testWidgets('tapping a tag toggles it into saved meta', (tester) async {
      final h = await _pump(tester, availableTags: ['EV6']);
      await _tapVisible(tester, find.text('EV6'));
      await _tapSave(tester);
      expect(h.saved!['tags'], ['EV6']);
    });

    testWidgets('a tag can be toggled back off', (tester) async {
      final h = await _pump(tester,
          availableTags: ['EV6'], initialMeta: {'tags': ['EV6']});
      await _tapVisible(tester, find.text('EV6')); // toggle off
      await _tapSave(tester);
      // Saved as an explicit empty list, not omitted — otherwise the day
      // would look like it has no tag data and silently re-inherit an
      // earlier day's tag on reload (issue #203).
      expect(h.saved!['tags'], isEmpty);
    });

    testWidgets('clearing an inherited tag down to zero saves an explicit '
        'empty list (issue #203)', (tester) async {
      final h = await _pump(tester, effectiveTags: ['EV6']);
      // 'EV6' renders twice: once in the hero, once as the pre-selected chip
      // in the Tags section below — the chip is the tappable one.
      await _tapVisible(tester, find.text('EV6').last);
      await _tapSave(tester);
      expect(h.saved!['tags'], isEmpty);
    });

    testWidgets('a new tag is added via the field + Add', (tester) async {
      final h = await _pump(tester);
      await tester.enterText(find.widgetWithText(TextField, 'New tag…'), 'Transfer');
      await _tapVisible(tester, find.text('Add'));
      await _tapSave(tester);
      expect(h.saved!['tags'], ['Transfer']);
    });

    testWidgets('a day with no own tags pre-selects the inherited (previous-day) tag',
        (tester) async {
      final h = await _pump(tester, effectiveTags: ['EV6']);
      // Pre-selection alone isn't a user edit — Save stays disabled until
      // something is actually changed.
      expect(_saveEnabled(tester), isFalse);

      // Once the day is edited for any reason, the pre-selected tag is
      // carried into the save as this day's own tag.
      await tester.tap(find.text('Hard'));
      await tester.pump();
      await _tapSave(tester);
      expect(h.saved!['tags'], ['EV6']);
    });
  });

  group('Counters', () {
    testWidgets('the + stepper increments the saved amount', (tester) async {
      final h = await _pump(tester,
          counters: [{'name': 'Snacks'}],
          initialMeta: {'counters': [{'name': 'Snacks', 'value': 3}]});

      expect(find.text('3'), findsOneWidget);
      await _tapVisible(tester, find.byKey(const ValueKey('ctr_inc_0_Snacks')));
      expect(find.text('4'), findsOneWidget);

      await _tapSave(tester);
      final counters = h.saved!['counters'] as List;
      expect(counters, [{'name': 'Snacks', 'value': 4.0}]);
    });

    testWidgets('legacy map counters still load', (tester) async {
      final h = await _pump(tester,
          counters: [{'name': 'Snacks'}],
          initialMeta: {'counters': {'Snacks': 3}}); // legacy {name: value} form
      expect(find.text('3'), findsOneWidget);
      await _tapVisible(tester, find.byKey(const ValueKey('ctr_inc_0_Snacks')));
      await _tapSave(tester);
      final counters = h.saved!['counters'] as List;
      expect(counters, [{'name': 'Snacks', 'value': 4.0}]);
    });

    testWidgets('"Add counter" offers every defined counter', (tester) async {
      final h = await _pump(tester, counters: [
        {'name': 'Snacks'},
        {'name': 'Coffees'},
      ]);
      await _tapVisible(tester, find.text('Add counter'));
      await tester.pumpAndSettle(); // menu opens
      expect(find.text('Snacks'), findsWidgets);
      expect(find.text('Coffees'), findsWidgets);
      await tester.tap(find.text('Coffees').last);
      await tester.pumpAndSettle();
      await _tapSave(tester);
      final counters = h.saved!['counters'] as List;
      expect(counters, [{'name': 'Coffees', 'value': 1.0}]);
    });

    testWidgets('the same counter can be added several times in one day',
        (tester) async {
      final h = await _pump(tester, counters: [{'name': 'Coffees'}]);
      // Add "Coffees" twice — it stays addable after the first add.
      await _tapVisible(tester, find.text('Add counter'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Coffees').last);
      await tester.pumpAndSettle();
      await _tapVisible(tester, find.text('Add counter'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Coffees').last);
      await tester.pumpAndSettle();
      // Bump the second row to 2 so the entries differ.
      await _tapVisible(tester, find.byKey(const ValueKey('ctr_inc_1_Coffees')));
      await _tapSave(tester);

      final counters = h.saved!['counters'] as List;
      expect(counters, [
        {'name': 'Coffees', 'value': 1.0},
        {'name': 'Coffees', 'value': 2.0},
      ]);
    });

    testWidgets('removing a counter drops it from saved meta', (tester) async {
      final h = await _pump(tester,
          counters: [{'name': 'Snacks'}],
          initialMeta: {'counters': [{'name': 'Snacks', 'value': 3}]});
      await _tapVisible(tester, find.byTooltip('Remove counter'));
      await _tapSave(tester);
      expect(h.saved!.containsKey('counters'), isFalse);
    });

    testWidgets('typing a value directly updates the saved amount', (tester) async {
      final h = await _pump(tester,
          counters: [{'name': 'Snacks'}],
          initialMeta: {'counters': [{'name': 'Snacks', 'value': 3}]});

      await tester.enterText(find.byKey(const ValueKey('ctr_val_0_Snacks')), '12');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      expect(find.text('12'), findsOneWidget);

      await _tapSave(tester);
      final counters = h.saved!['counters'] as List;
      expect(counters, [{'name': 'Snacks', 'value': 12.0}]);
    });

    testWidgets('a comma decimal separator is accepted', (tester) async {
      final h = await _pump(tester,
          counters: [{'name': 'Snacks'}],
          initialMeta: {'counters': [{'name': 'Snacks', 'value': 3}]});

      await tester.enterText(find.byKey(const ValueKey('ctr_val_0_Snacks')), '3,5');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      expect(find.text('3.5'), findsOneWidget);

      await _tapSave(tester);
      final counters = h.saved!['counters'] as List;
      expect(counters, [{'name': 'Snacks', 'value': 3.5}]);
    });

    testWidgets('invalid text reverts to the previous value', (tester) async {
      await _pump(tester,
          counters: [{'name': 'Snacks'}],
          initialMeta: {'counters': [{'name': 'Snacks', 'value': 3}]});

      await tester.enterText(find.byKey(const ValueKey('ctr_val_0_Snacks')), 'abc');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('3'), findsOneWidget);
      expect(_saveEnabled(tester), isFalse); // rejected edit, still clean
    });

    testWidgets('the field stays in sync when the +/- steppers change the value',
        (tester) async {
      await _pump(tester,
          counters: [{'name': 'Snacks'}],
          initialMeta: {'counters': [{'name': 'Snacks', 'value': 3}]});

      await _tapVisible(tester, find.byKey(const ValueKey('ctr_inc_0_Snacks')));
      expect(find.text('4'), findsOneWidget);

      final field = tester.widget<TextField>(
        find.byKey(const ValueKey('ctr_val_0_Snacks')));
      expect(field.controller!.text, '4');
    });
  });

  group('Footer', () {
    testWidgets('empty + clean shows "Nothing to save yet" with Save disabled',
        (tester) async {
      await _pump(tester);
      expect(find.text('Nothing to save yet'), findsOneWidget);
      expect(_saveEnabled(tester), isFalse);
    });

    testWidgets('an edit flips the status to "Unsaved changes" and enables Save',
        (tester) async {
      await _pump(tester);
      await tester.tap(find.text('Normal'));
      await tester.pump();
      expect(find.text('Unsaved changes'), findsOneWidget);
      expect(_saveEnabled(tester), isTrue);
    });
  });

  group('countersOnly variant (add-FAB counter shortcut)', () {
    testWidgets('shows the hero and counters but hides the other sections',
        (tester) async {
      await _pump(
        tester,
        counters: [{'name': 'Coffees', 'start': 0}],
        countersOnly: true,
      );
      // Hero retained (the day's ISO date is part of the header).
      expect(find.text('2026-06-14'), findsWidgets);
      // Counters section present (section labels render upper-cased)...
      expect(find.text('COUNTERS'), findsOneWidget);
      // ...and every other body section is gone.
      expect(find.text('DIFFICULTY'), findsNothing);
      expect(find.text('SLEEPING'), findsNothing);
      expect(find.text('TAGS'), findsNothing);
      expect(find.text('JOURNAL'), findsNothing);
    });

    testWidgets('guides the user when the project defines no counters',
        (tester) async {
      await _pump(tester, counters: const [], countersOnly: true);
      expect(find.textContaining('no counters yet'), findsOneWidget);
      expect(find.text('DIFFICULTY'), findsNothing);
    });
  });
}
