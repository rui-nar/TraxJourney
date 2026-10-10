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

  // The hero map's label and memory card sit in a clipping Stack, which cuts
  // them off without reporting an overflow. They were placed by screen width,
  // so on a wide window they landed past the half-screen map (review R1-1).
  for (final size in [..._phones, const Size(1280, 900), const Size(1920, 1080)]) {
    testWidgets('the hero map keeps its label and card inside it at '
        '${size.width.toInt()} px', (tester) async {
      await _pump(tester, size, 1.0);
      final label = find.text('Barcelos');
      final map = tester.getRect(
          find.ancestor(of: label, matching: find.byType(Stack)).first);
      for (final item in [label, find.text('Ponte de Lima café')]) {
        final placed = tester.getRect(
            find.ancestor(of: item, matching: find.byType(Positioned)).first);
        expect(map.contains(placed.topLeft) && map.contains(placed.bottomRight - const Offset(1, 1)),
            isTrue, reason: '$placed is not inside the map $map');
      }
    });
  }

  // "· AGPL" moves under "Free" as one piece; the dot never ends a line on
  // its own (review R1-2).
  for (final phone in _phones) {
    for (final scale in [1.0, 2.0, 3.0]) {
      testWidgets('"· AGPL" stays together at ${phone.width.toInt()} px and '
          '${scale}x text', (tester) async {
        await _pump(tester, phone, scale);
        final p = tester.renderObject<RenderParagraph>(find.byWidgetPredicate(
            (w) => w is RichText && w.text.toPlainText().startsWith('Free')));
        final text = p.text.toPlainText();
        final dot = text.indexOf('·');
        final tops = {
          for (final box in p.getBoxesForSelection(
              TextSelection(baseOffset: dot, extentOffset: text.length)))
            box.top.round(),
        };
        expect(tops, hasLength(1), reason: '"$text" breaks inside "· AGPL"');
      });
    }
  }

  // Every section shares the nav's side gutter: 16 px on a phone (review R1-3).
  for (final phone in _phones) {
    testWidgets('sections line up with the nav at ${phone.width.toInt()} px',
        (tester) async {
      await _pump(tester, phone, 1.0);
      // The features section's eyebrow (the footer's link comes later; on a
      // phone the nav has no link of that name).
      expect(tester.getTopLeft(find.text('Features').first).dx, 16);
      expect(tester.getTopLeft(find.text(kWordmarkLead).first).dx,
          lessThan(16 + 28 + 10 + 1)); // logo (28) and its gap (10) first
    });
  }

  // Everything is measured in a font loadRealFonts loaded and checked: text
  // in any other family would lay out in the test font (review R1-4).
  testWidgets('the page draws only fonts the test loaded', (tester) async {
    await _pump(tester, const Size(390, 844), 1.0);
    final families = <String?>{};
    for (final p in tester.renderObjectList<RenderParagraph>(find.descendant(
        of: find.byType(WelcomeScreen), matching: find.byType(RichText)))) {
      p.text.visitChildren((span) {
        if (span is TextSpan && span.text != null) {
          families.add(span.style?.fontFamily);
        }
        return true;
      });
    }
    families
      ..remove('MaterialIcons') // icon glyphs, not text
      ..remove('Inter'); // the map labels' unregistered family: issue #594
    expect(families.difference(loadedFontFamilies), isEmpty);
  });

  testWidgets('the wordmark spells the app name', (tester) async {
    await _pump(tester, const Size(390, 844), 1.0);
    expect('$kWordmarkLead$kWordmarkTail', kAppName);
    expect(find.text(kWordmarkLead), findsWidgets);
    expect(find.text(kWordmarkTail), findsWidgets);
  });
}
