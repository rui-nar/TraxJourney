// The landing page on phones (issue #473).
//
// Laid out in the real fonts (test/helpers/real_fonts.dart): with the test
// font every glyph is a full em wide and the page overflows for that reason
// alone. So an overflow reported here is one a reader sees.
//
// At the reader's default text size nothing on the page may overflow. At 2x
// and 3x the pricing cards must hold; the rest of the page at those sizes is
// issue #595.

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/auth/welcome_screen.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/core/brand.dart';

import '../helpers/real_fonts.dart';

/// Small phone (iPhone SE 1st gen), common Android, common iPhone.
const _phones = [Size(320, 568), Size(360, 800), Size(390, 844)];

/// Each pricing card's column, found by its tagline.
const _cardTaglines = [
  'We run the Docker, you ride the bike.',
  'For makers with their own hardware.',
];

/// Pumps the page at [phone] and [scale], returning the flexes that reported
/// an overflow while it was laid out.
Future<List<RenderObject>> _pump(
    WidgetTester tester, Size phone, double scale) async {
  tester.view.physicalSize = phone;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  ignoreFontFetchErrors();

  final overflowing = <RenderObject>[];
  final previous = FlutterError.onError;
  FlutterError.onError = (details) {
    if (!details.exceptionAsString().contains('overflowed')) {
      return previous?.call(details);
    }
    for (final node in details.informationCollector?.call() ??
        const <DiagnosticsNode>[]) {
      if (node.value is RenderObject) {
        overflowing.add(node.value as RenderObject);
        return;
      }
    }
    previous?.call(details); // an overflow we cannot place: fail loudly
  };
  addTearDown(() => FlutterError.onError = previous);

  // A plan for sale, so the hosted card shows its price line too.
  await tester.pumpWidget(MaterialApp(
      theme: landingTheme(),
      home: WelcomeScreen(loadPlans: () async => [
            const PlanInfo(id: 'tier_1', name: 'Tier 1',
                priceLabel: '€0.99 / month', features: [], purchasable: true),
          ])));
  await tester.pump();
  await tester.pump();
  FlutterError.onError = previous;
  return overflowing;
}

bool _inside(RenderObject child, RenderObject ancestor) {
  for (RenderObject? o = child; o != null; o = o.parent) {
    if (identical(o, ancestor)) return true;
  }
  return false;
}

String _describe(List<RenderObject> flexes) =>
    flexes.map((f) => f.debugCreator.toString()).join('\n');

void main() {
  setUpAll(loadRealFonts);

  for (final size in [..._phones, const Size(1280, 900)]) {
    testWidgets('nothing overflows at ${size.width.toInt()} px', (tester) async {
      final overflowing = await _pump(tester, size, 1.0);
      expect(overflowing, isEmpty, reason: _describe(overflowing));
    });
  }

  for (final phone in _phones) {
    for (final scale in [2.0, 3.0]) {
      testWidgets(
          'the pricing cards hold at ${phone.width.toInt()} px and ${scale}x text',
          (tester) async {
        final overflowing = await _pump(tester, phone, scale);
        for (final tagline in _cardTaglines) {
          final card = tester.renderObject(find
              .ancestor(of: find.text(tagline), matching: find.byType(Column))
              .first);
          final inCard = [
            for (final f in overflowing) if (_inside(f, card)) f,
          ];
          expect(inCard, isEmpty, reason: '"$tagline" card:\n${_describe(inCard)}');
        }
        // The hosted card was laid out with its feature lines.
        expect(find.text('Same app, hosted for you'), findsOneWidget);
      });
    }
  }

  // A word wider than its line does not overflow: it is broken between two
  // letters, which no overflow check sees. The hero headline was, on every
  // phone ("advent / ure,").
  for (final size in [..._phones, const Size(412, 915), const Size(1280, 900)]) {
    testWidgets('no word is split across lines at ${size.width.toInt()} px',
        (tester) async {
      await _pump(tester, size, 1.0);
      final split = <String>[];
      for (final p in tester.renderObjectList<RenderParagraph>(find.descendant(
          of: find.byType(WelcomeScreen), matching: find.byType(RichText)))) {
        final text = p.text.toPlainText();
        // Breaking at a space or after a hyphen is fine.
        for (final word in RegExp(r'[^\s-]+').allMatches(text)) {
          final tops = {
            for (final box in p.getBoxesForSelection(TextSelection(
                baseOffset: word.start, extentOffset: word.end)))
              box.top.round(),
          };
          if (tops.length > 1) split.add('"${word[0]}" in "$text"');
        }
      }
      expect(split, isEmpty);
    });
  }

  testWidgets('the wordmark spells the app name', (tester) async {
    await _pump(tester, const Size(390, 844), 1.0);
    expect('$kWordmarkLead$kWordmarkTail', kAppName);
    expect(find.text(kWordmarkLead), findsWidgets);
    expect(find.text(kWordmarkTail), findsWidgets);
  });
}
