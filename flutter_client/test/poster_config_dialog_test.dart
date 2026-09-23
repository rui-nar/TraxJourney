// Widget tests for PosterConfigDialog (mirrors photo_upgrade_screen_test.dart's
// pumpWidget/showDialog/tap style): confirm each checkbox toggles its own
// field and the confirm callback receives the expected options object.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/poster_config_dialog.dart';

const _defaults = PosterConfigOptions(
  distance: true,
  elevation: true,
  heroPhoto: true,
  allPhotos: false,
  memoryText: true,
  counters: true,
  tagPie: false,
  encounters: false,
  tripSummary: true,
  theme: 'dark',
  layout: 'radial',
);

/// Every checkbox that is on by default and feeds the *memory* card — turning
/// all of these off leaves that card with nothing on it. ('Trip summary card'
/// is left out on purpose: it is a separate, standalone card.)
const _memoryDefaultsOn = [
  'Distance',
  'Elevation',
  'Hero photo',
  'Memory text',
  'Counters',
];

/// Opens the dialog and applies [selectTheme]/[toggleTitles], leaving it open
/// so a test can either inspect the live preview or press an action itself.
Future<void> _openDialog(
  WidgetTester tester, {
  List<String> toggleTitles = const [],
  String? selectTheme,
  String? selectLayout,
  void Function(PosterConfigOptions)? onConfirm,
}) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => Center(
          child: ElevatedButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => PosterConfigDialog(
                onConfirm: (opts) => onConfirm?.call(opts),
              ),
            ),
            child: const Text('open'),
          ),
        ),
      ),
    ),
  ));

  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();

  if (selectTheme != null) {
    await tester.tap(find.text(selectTheme));
    await tester.pump();
  }

  if (selectLayout != null) {
    await tester.tap(find.text(selectLayout));
    await tester.pump();
  }

  for (final title in toggleTitles) {
    // The dialog's option list scrolls (SingleChildScrollView) and now runs
    // longer than the test surface's height since the theme picker was
    // added above it, so a later checkbox can be off-screen — scroll it
    // into view before tapping rather than assuming it's already visible.
    final finder = find.widgetWithText(CheckboxListTile, title);
    await tester.ensureVisible(finder);
    await tester.pumpAndSettle();
    await tester.tap(finder);
    await tester.pump();
  }
}

Future<PosterConfigOptions?> _openAndConfirm(
  WidgetTester tester, {
  List<String> toggleTitles = const [],
  String? selectTheme,
  String? selectLayout,
  bool cancel = false,
}) async {
  PosterConfigOptions? result;
  await _openDialog(
    tester,
    toggleTitles: toggleTitles,
    selectTheme: selectTheme,
    selectLayout: selectLayout,
    onConfirm: (opts) => result = opts,
  );

  // "Preview" is ambiguous with the live preview card's own section heading
  // (poster_preview_card.dart) — target the action button specifically.
  await tester.tap(cancel
      ? find.text('Cancel')
      : find.widgetWithText(FilledButton, 'Preview'));
  await tester.pumpAndSettle();

  return result;
}

void main() {
  testWidgets('confirming with no changes returns the default options',
      (tester) async {
    final result = await _openAndConfirm(tester);

    expect(result, isNotNull);
    expect(result!.toJson(), _defaults.toJson());
  });

  testWidgets('cancel does not invoke the confirm callback', (tester) async {
    final result = await _openAndConfirm(tester, cancel: true);
    expect(result, isNull);
  });

  testWidgets('toggling two checkboxes flips only those fields',
      (tester) async {
    final result = await _openAndConfirm(
      tester,
      toggleTitles: ['All photos', 'Distance'],
    );

    expect(result, isNotNull);
    expect(result!.allPhotos, isTrue); // default false -> toggled on
    expect(result.distance, isFalse); // default true -> toggled off
    // Untouched fields keep their defaults.
    expect(result.elevation, _defaults.elevation);
    expect(result.heroPhoto, _defaults.heroPhoto);
    expect(result.memoryText, _defaults.memoryText);
    expect(result.counters, _defaults.counters);
    expect(result.tagPie, _defaults.tagPie);
    expect(result.encounters, _defaults.encounters);
    expect(result.tripSummary, _defaults.tripSummary);
    expect(result.theme, _defaults.theme);
  });

  testWidgets('trip summary defaults to on and is sent as trip_summary',
      (tester) async {
    final result = await _openAndConfirm(tester);

    expect(result, isNotNull);
    expect(result!.tripSummary, isTrue);
    expect(result.toJson()['trip_summary'], isTrue);
  });

  testWidgets('turning the trip summary card off flips only that field',
      (tester) async {
    final result =
        await _openAndConfirm(tester, toggleTitles: ['Trip summary card']);

    expect(result, isNotNull);
    expect(result!.tripSummary, isFalse);
    expect(result.toJson()['trip_summary'], isFalse);
    expect(result.distance, _defaults.distance);
    expect(result.memoryText, _defaults.memoryText);
    expect(result.theme, _defaults.theme);
    expect(result.layout, _defaults.layout);
  });

  testWidgets('theme defaults to dark', (tester) async {
    final result = await _openAndConfirm(tester);

    expect(result, isNotNull);
    expect(result!.theme, 'dark');
    expect(result.toJson()['theme'], 'dark');
  });

  testWidgets('selecting "Light" updates the theme field and toJson',
      (tester) async {
    final result = await _openAndConfirm(tester, selectTheme: 'Light');

    expect(result, isNotNull);
    expect(result!.theme, 'light');
    expect(result.toJson()['theme'], 'light');
    // Other fields keep their defaults.
    expect(result.distance, _defaults.distance);
    expect(result.elevation, _defaults.elevation);
    expect(result.heroPhoto, _defaults.heroPhoto);
    expect(result.allPhotos, _defaults.allPhotos);
    expect(result.memoryText, _defaults.memoryText);
    expect(result.counters, _defaults.counters);
    expect(result.tagPie, _defaults.tagPie);
    expect(result.encounters, _defaults.encounters);
    expect(result.layout, _defaults.layout);
    expect(result.tripSummary, _defaults.tripSummary);
  });

  testWidgets('layout defaults to radial', (tester) async {
    final result = await _openAndConfirm(tester);

    expect(result, isNotNull);
    expect(result!.layout, 'radial');
    expect(result.toJson()['layout'], 'radial');
  });

  testWidgets('selecting "Perimeter" updates the layout field and toJson',
      (tester) async {
    final result = await _openAndConfirm(tester, selectLayout: 'Perimeter');

    expect(result, isNotNull);
    expect(result!.layout, 'perimeter');
    expect(result.toJson()['layout'], 'perimeter');
    // Other fields keep their defaults.
    expect(result.distance, _defaults.distance);
    expect(result.elevation, _defaults.elevation);
    expect(result.heroPhoto, _defaults.heroPhoto);
    expect(result.allPhotos, _defaults.allPhotos);
    expect(result.memoryText, _defaults.memoryText);
    expect(result.counters, _defaults.counters);
    expect(result.tagPie, _defaults.tagPie);
    expect(result.encounters, _defaults.encounters);
    expect(result.theme, _defaults.theme);
    expect(result.tripSummary, _defaults.tripSummary);
  });

  // ── Live preview ──────────────────────────────────────────────────
  // Light checks that the mock-up follows the toggles; it is a visual
  // approximation, so nothing here asserts on its layout or measurements.

  testWidgets('preview shows a titled, day-badged card by default',
      (tester) async {
    await _openDialog(tester);

    expect(find.text('DAY 3'), findsOneWidget);
    expect(find.text('Sunset at the beach'), findsOneWidget);
  });

  testWidgets('preview keeps the title when memory text is off',
      (tester) async {
    // The title is no longer tied to the memory_text toggle; only the date
    // and description lines are.
    await _openDialog(tester, toggleTitles: ['Memory text']);

    expect(find.text('Sunset at the beach'), findsOneWidget);
    expect(find.text('DAY 3'), findsOneWidget);
    expect(find.text('12 Jun 2024'), findsNothing);
  });

  testWidgets('preview shows no card when every memory element is off',
      (tester) async {
    await _openDialog(tester, toggleTitles: _memoryDefaultsOn);

    expect(find.textContaining('no card'), findsOneWidget);
    expect(find.text('DAY 3'), findsNothing);
    expect(find.text('Sunset at the beach'), findsNothing);
  });

  testWidgets('preview mocks up the trip summary card while it is on',
      (tester) async {
    await _openDialog(tester);

    expect(find.text('Iceland ring road'), findsOneWidget);
    expect(find.text('1 Jun 2024 – 14 Jun 2024'), findsOneWidget);
  });

  testWidgets('preview drops the trip summary card when it is off',
      (tester) async {
    await _openDialog(tester, toggleTitles: ['Trip summary card']);

    expect(find.text('Iceland ring road'), findsNothing);
    // The memory card is untouched by that toggle.
    expect(find.text('Sunset at the beach'), findsOneWidget);
  });

  // Every checkbox title maps 1:1 to a PosterConfigOptions.toJson() key.
  const fieldsByTitle = {
    'Distance': 'distance',
    'Elevation': 'elevation',
    'Hero photo': 'hero_photo',
    'All photos': 'all_photos',
    'Memory text': 'memory_text',
    'Counters': 'counters',
    'Tag pie chart': 'tag_pie',
    'Number of encounters': 'encounters',
    'Trip summary card': 'trip_summary',
  };

  for (final entry in fieldsByTitle.entries) {
    testWidgets('toggling "${entry.key}" flips only the ${entry.value} field',
        (tester) async {
      final result = await _openAndConfirm(tester, toggleTitles: [entry.key]);
      expect(result, isNotNull);

      final defaultsJson = _defaults.toJson();
      final actualJson = result!.toJson();
      for (final key in defaultsJson.keys) {
        if (key == entry.value) {
          expect(actualJson[key], !defaultsJson[key]!,
              reason: '$key should have flipped');
        } else {
          expect(actualJson[key], defaultsJson[key],
              reason: '$key should be unchanged');
        }
      }
    });
  }
}
