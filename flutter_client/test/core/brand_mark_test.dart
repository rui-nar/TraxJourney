// Tests for the brand mark painter (lib/src/core/brand_mark.dart).
//
// The mark's appearance is not asserted here — that is what the design board is
// for, and a golden would pin down antialiasing rather than intent. What is
// worth locking down is that it paints at all: the geometry uses arcToPoint, a
// gradient shader and a MaskFilter, any of which throws on bad arguments (a
// radius too small for its chord, a zero-length gradient axis, a negative
// sigma) rather than degrading quietly.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/brand_mark.dart';

void main() {
  group('BrandMark', () {
    testWidgets('takes exactly the square it is given', (tester) async {
      await tester.pumpWidget(
        const Directionality(
          textDirection: TextDirection.ltr,
          child: Center(child: BrandMark(size: 168)),
        ),
      );

      expect(tester.getSize(find.byType(BrandMark)), const Size(168, 168));
    });

    testWidgets('paints at every size the app asks it for', (tester) async {
      // 32 is the favicon, 168 the stacked splash, 228 the wide one.
      for (final size in <double>[32, 168, 228, 512]) {
        await tester.pumpWidget(
          Directionality(
            textDirection: TextDirection.ltr,
            child: Center(child: BrandMark(size: size)),
          ),
        );
        await tester.pump();

        expect(tester.takeException(), isNull, reason: 'failed at $size px');
      }
    });

    testWidgets('survives a non-square box by using the shorter side',
        (tester) async {
      await tester.pumpWidget(
        const Directionality(
          textDirection: TextDirection.ltr,
          child: SizedBox(
            width: 300,
            height: 80,
            child: CustomPaint(painter: BrandMarkPainter()),
          ),
        ),
      );

      expect(tester.takeException(), isNull);
    });
  });

  group('BrandMarkPainter', () {
    test('never needs repainting — the geometry is fixed', () {
      expect(
        const BrandMarkPainter().shouldRepaint(const BrandMarkPainter()),
        isFalse,
      );
    });
  });
}
