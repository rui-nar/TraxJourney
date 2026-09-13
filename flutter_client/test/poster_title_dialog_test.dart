// Widget tests for PosterTitleDialog (mirrors poster_config_dialog_test.dart's
// pumpWidget/showDialog/tap style): confirm dragging the title box, editing
// its text and moving the slider all feed the expected options object, and
// that its toJson() matches api/poster.py's PosterRequest field names.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/poster_title_dialog.dart';

/// Opens the dialog, leaving it open so a test can drag/type/slide before
/// pressing an action itself.
Future<void> _openDialog(
  WidgetTester tester, {
  String orientation = 'landscape',
  String initialTitle = 'My Trip',
  void Function(PosterTitleOptions)? onConfirm,
}) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => Center(
          child: ElevatedButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => PosterTitleDialog(
                orientation: orientation,
                initialTitle: initialTitle,
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
}

Future<PosterTitleOptions?> _openAndConfirm(
  WidgetTester tester, {
  String orientation = 'landscape',
  String initialTitle = 'My Trip',
  bool cancel = false,
}) async {
  PosterTitleOptions? result;
  await _openDialog(
    tester,
    orientation: orientation,
    initialTitle: initialTitle,
    onConfirm: (opts) => result = opts,
  );

  await tester.tap(cancel
      ? find.text('Cancel')
      : find.widgetWithText(FilledButton, 'Preview'));
  await tester.pumpAndSettle();

  return result;
}

void main() {
  testWidgets('confirming with no changes returns the default position/scale '
      'and the project name as the title', (tester) async {
    final result = await _openAndConfirm(tester);

    expect(result, isNotNull);
    expect(result!.positionX, 0.0);
    expect(result.positionY, 0.0);
    expect(result.titleText, 'My Trip');
    expect(result.titleScale, 1.0);
  });

  testWidgets('the title field is pre-filled with the project name',
      (tester) async {
    await _openDialog(tester, initialTitle: 'Iceland Ring Road');
    expect(find.text('Iceland Ring Road'), findsOneWidget);
  });

  testWidgets('cancel does not invoke the confirm callback', (tester) async {
    final result = await _openAndConfirm(tester, cancel: true);
    expect(result, isNull);
  });

  testWidgets('editing the title text overrides it', (tester) async {
    PosterTitleOptions? result;
    await _openDialog(tester, onConfirm: (opts) => result = opts);

    await tester.enterText(find.byType(TextField), 'A smaller slice');
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Preview'));
    await tester.pumpAndSettle();

    expect(result, isNotNull);
    final nonNull = result!;
    expect(nonNull.titleText, 'A smaller slice');
    // Untouched fields keep their defaults.
    expect(nonNull.positionX, 0.0);
    expect(nonNull.positionY, 0.0);
    expect(nonNull.titleScale, 1.0);
  });

  testWidgets('clearing the title text sends null, not an empty string',
      (tester) async {
    PosterTitleOptions? result;
    await _openDialog(tester, onConfirm: (opts) => result = opts);

    await tester.enterText(find.byType(TextField), '   ');
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Preview'));
    await tester.pumpAndSettle();

    expect(result, isNotNull);
    expect(result!.titleText, isNull);
  });

  testWidgets('dragging the title box updates its normalized position',
      (tester) async {
    PosterTitleOptions? result;
    await _openDialog(tester, onConfirm: (opts) => result = opts);

    final dragHandle = find.byIcon(Icons.drag_indicator);
    expect(dragHandle, findsOneWidget);
    // The dialog's content scrolls (SingleChildScrollView), so the drag box
    // can be off-screen until scrolled into view first.
    await tester.ensureVisible(dragHandle);
    await tester.pumpAndSettle();
    // Drag towards the bottom-right; the box is clamped within the outline,
    // so a large drag lands it at (close to) the far corner.
    await tester.drag(dragHandle, const Offset(2000, 2000));
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Preview'));
    await tester.pumpAndSettle();

    expect(result, isNotNull);
    final nonNull = result!;
    expect(nonNull.positionX, 1.0);
    expect(nonNull.positionY, 1.0);
  });

  testWidgets('moving the slider changes titleScale within its bounds',
      (tester) async {
    PosterTitleOptions? result;
    await _openDialog(tester, onConfirm: (opts) => result = opts);

    final sliderFinder = find.byType(Slider);
    expect(sliderFinder, findsOneWidget);
    final slider = tester.widget<Slider>(sliderFinder);
    expect(slider.min, kTitleScaleMin);
    expect(slider.max, kTitleScaleMax);

    await tester.ensureVisible(sliderFinder);
    await tester.pumpAndSettle();
    await tester.drag(sliderFinder, const Offset(1000, 0)); // drag to max
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Preview'));
    await tester.pumpAndSettle();

    expect(result, isNotNull);
    expect(result!.titleScale, kTitleScaleMax);
  });

  group('toJson', () {
    test('matches PosterRequest field names in api/poster.py', () {
      const opts = PosterTitleOptions(
        positionX: 0.25,
        positionY: 0.75,
        titleText: 'Alps Only',
        titleScale: 1.5,
      );
      expect(opts.toJson(), {
        'title_position': {'x': 0.25, 'y': 0.75},
        'title_text': 'Alps Only',
        'title_scale': 1.5,
      });
    });

    test('a null titleText serializes to null', () {
      const opts = PosterTitleOptions();
      expect(opts.toJson()['title_text'], isNull);
    });
  });
}
