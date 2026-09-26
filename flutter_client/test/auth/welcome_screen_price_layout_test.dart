// The hosted card's price on real phone widths and text sizes (issue #432).
//
// Widget tests normally draw every glyph a full em wide, which says nothing
// about where real text wraps. This file loads the real Inter ExtraBold (the
// price's weight; OFL, see test/fonts/Inter-LICENSE.txt) under the family name
// google_fonts asks for, so the layout below is the one a reader gets.

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:traxjourney_client/src/auth/welcome_screen.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';

const _label = '€0.99 / month';

Finder _cloudCard() =>
    find.ancestor(of: find.text('Cloud'), matching: find.byType(Stack)).first;

/// The characters of [label] grouped into the lines they were laid out on.
List<String> _lines(RenderParagraph paragraph, String label) {
  final byTop = <double, StringBuffer>{};
  for (var i = 0; i < label.length; i++) {
    final boxes = paragraph.getBoxesForSelection(
        TextSelection(baseOffset: i, extentOffset: i + 1));
    if (boxes.isEmpty) continue;
    byTop.putIfAbsent(boxes.first.top.roundToDouble(), StringBuffer.new)
        .write(label[i]);
  }
  final tops = byTop.keys.toList()..sort();
  return [for (final t in tops) byTop[t].toString()];
}

void main() {
  setUpAll(() async {
    GoogleFonts.config.allowRuntimeFetching = false;
    final bytes = File('test/fonts/Inter-ExtraBold.ttf').readAsBytesSync();
    final loader =
        FontLoader(GoogleFonts.inter(fontWeight: FontWeight.w800).fontFamily!)
          ..addFont(Future.value(ByteData.sublistView(bytes)));
    await loader.load();
  });

  for (final width in [320.0, 360.0, 390.0]) {
    for (final scale in [1.0, 2.0, 3.0]) {
      testWidgets('price at ${width.toInt()} px and ${scale}x text',
          (tester) async {
        tester.view.physicalSize = Size(width, 3000);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        tester.platformDispatcher.textScaleFactorTestValue = scale;
        addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);

        // Only the price is under test: the rest of the page (other weights,
        // feature rows at large text) is not, so its font lookups and
        // overflow reports are set aside.
        final previous = FlutterError.onError;
        FlutterError.onError = (details) {
          final text = details.exceptionAsString();
          if (text.contains('overflowed') || text.contains('GoogleFonts') ||
              text.contains('google_fonts') ||
              text.contains('allowRuntimeFetching')) {
            return;
          }
          previous?.call(details);
        };
        addTearDown(() => FlutterError.onError = previous);

        await tester.pumpWidget(MaterialApp(
            home: WelcomeScreen(loadPlans: () async => [
                  const PlanInfo(id: 'tier_1', name: 'Tier 1',
                      priceLabel: _label, features: [], purchasable: true),
                ])));
        await tester.pump();
        await tester.pump();

        final price = find.descendant(
            of: _cloudCard(),
            matching: find.byWidgetPredicate((w) =>
                w is RichText &&
                w.text.toPlainText().replaceAll(' ', ' ') == _label));
        expect(price, findsOneWidget);
        final paragraph = tester.renderObject<RenderParagraph>(price);
        // What was actually laid out: the label, unless the widget rewrote it.
        final text = paragraph.text.toPlainText();

        // The reader's text size reaches the price, capped at 2x.
        final applied = scale > 2 ? 2.0 : scale;
        expect(paragraph.textScaler.scale(28), moreOrLessEquals(28 * applied));

        // Every line break falls at a (breakable) space: never inside "€0.99"
        // or "month".
        final lines = _lines(paragraph, text);
        var at = 0;
        for (final line in lines.skip(1)) {
          at = text.indexOf(line, at + 1);
          expect(text[at - 1], ' ', reason: 'broke before "$line" in $lines');
        }

        // Nothing is clipped: every visible glyph sits inside the paragraph,
        // and the paragraph on screen.
        for (var i = 0; i < text.length; i++) {
          if (text[i].trim().isEmpty) continue;
          for (final box in paragraph.getBoxesForSelection(
              TextSelection(baseOffset: i, extentOffset: i + 1))) {
            expect(box.right, lessThanOrEqualTo(paragraph.size.width + 0.5),
                reason: '"${text[i]}" is clipped');
          }
        }
        expect(tester.getRect(price).right, lessThanOrEqualTo(width));
      });
    }
  }
}
