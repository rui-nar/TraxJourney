import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/scrolling_selectable_text.dart';

/// Regression coverage for the activity-panel marquee: only overflowing text
/// scrolls, only while selected, the sequence runs once and holds (never
/// loops forever — see WCAG 2.2.2), and the web-only tooltip is driven by an
/// injectable flag rather than the real (untestable) kIsWeb constant.
void main() {
  const marqueeKey = ValueKey('scrolling-text-marquee-active');
  const shortText = 'Run';
  const longText = 'A remarkably long activity name that will not fit here';

  Widget harness({
    required String text,
    required bool isSelected,
    double width = 100,
    bool showTooltipOnWeb = false,
    TextDirection textDirection = TextDirection.ltr,
  }) {
    return MaterialApp(
      home: Scaffold(
        body: Directionality(
          textDirection: textDirection,
          child: SizedBox(
            width: width,
            child: ScrollingSelectableText(
              text: text,
              isSelected: isSelected,
              showTooltipOnWeb: showTooltipOnWeb,
              pixelsPerSecond: 1000,
              pauseDuration: const Duration(milliseconds: 10),
            ),
          ),
        ),
      ),
    );
  }

  double transformDx(WidgetTester tester) => tester
      .widget<Transform>(find.descendant(
          of: find.byKey(marqueeKey), matching: find.byType(Transform)))
      .transform
      .getTranslation()
      .x;

  testWidgets('short text never animates, even when selected', (tester) async {
    await tester.pumpWidget(
        harness(text: shortText, isSelected: true, width: 200));
    await tester.pump();

    expect(find.byKey(marqueeKey), findsNothing);
    expect(
        tester
            .widget<Text>(find.text(shortText))
            .overflow,
        TextOverflow.ellipsis);
  });

  testWidgets('long text, not selected: static ellipsis, no animation',
      (tester) async {
    await tester.pumpWidget(harness(text: longText, isSelected: false));
    await tester.pump();

    expect(find.byKey(marqueeKey), findsNothing);
    expect(
        tester
            .widget<Text>(find.text(longText))
            .overflow,
        TextOverflow.ellipsis);
  });

  testWidgets('long text, selected: animates and terminates (no infinite loop)',
      (tester) async {
    await tester.pumpWidget(harness(text: longText, isSelected: true));
    await tester.pump();

    expect(find.byKey(marqueeKey), findsOneWidget);

    // Mid-scroll: should be moving (non-zero offset).
    await tester.pump(const Duration(milliseconds: 610));
    expect(transformDx(tester), isNot(0.0));

    // pumpAndSettle must complete — an indefinitely-repeating animation
    // would time out here. It should end back at the start position.
    await tester.pumpAndSettle();
    expect(transformDx(tester), 0.0);
  });

  testWidgets('deselecting mid-scroll resets to static ellipsis',
      (tester) async {
    await tester.pumpWidget(harness(text: longText, isSelected: true));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 610));
    expect(find.byKey(marqueeKey), findsOneWidget);

    await tester.pumpWidget(harness(text: longText, isSelected: false));
    await tester.pump();

    expect(find.byKey(marqueeKey), findsNothing);
  });

  testWidgets('RTL mirrors the scroll direction', (tester) async {
    await tester.pumpWidget(harness(
        text: longText, isSelected: true, textDirection: TextDirection.rtl));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 610));

    expect(transformDx(tester), greaterThan(0.0));
  });

  group('web tooltip (injectable flag, independent of isSelected)', () {
    testWidgets('shown when showTooltipOnWeb and text overflows',
        (tester) async {
      await tester.pumpWidget(harness(
          text: longText, isSelected: false, showTooltipOnWeb: true));
      await tester.pump();

      final tooltip = tester.widget<Tooltip>(find.byType(Tooltip));
      expect(tooltip.message, longText);
    });

    testWidgets('hidden when showTooltipOnWeb is false, even if overflowing',
        (tester) async {
      await tester.pumpWidget(harness(
          text: longText, isSelected: false, showTooltipOnWeb: false));
      await tester.pump();

      expect(find.byType(Tooltip), findsNothing);
    });

    testWidgets('hidden when text does not overflow, even if showTooltipOnWeb',
        (tester) async {
      await tester.pumpWidget(harness(
          text: shortText,
          isSelected: false,
          width: 200,
          showTooltipOnWeb: true));
      await tester.pump();

      expect(find.byType(Tooltip), findsNothing);
    });
  });
}
