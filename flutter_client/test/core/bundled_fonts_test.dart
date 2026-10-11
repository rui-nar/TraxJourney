// The app's fonts are bundled, the same on every platform (#601, #602).
//
// Fetched from Google Fonts at run time, each weight was its own family and
// theme text drew bold from the Regular file (#601). Text asking for the
// generic 'monospace' family got a monospace font on the web and Android but
// the proportional system font on iOS, which has no family by that name
// (#602). Both typefaces now ship in pubspec.yaml.

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/design_tokens.dart';

import '../helpers/real_fonts.dart';

double _width(String text, String family) {
  final painter = TextPainter(
    text: TextSpan(text: text, style: TextStyle(fontFamily: family, fontSize: 10)),
    textDirection: TextDirection.ltr,
  )..layout();
  final width = painter.width;
  painter.dispose();
  return width;
}

void main() {
  setUpAll(loadRealFonts);

  test('both typefaces are bundled, at every weight the app uses', () {
    final fonts = bundledFonts();
    expect(fonts.keys, containsAll([kFontFamily, kMonoFontFamily]));
    for (final family in [kFontFamily, kMonoFontFamily]) {
      expect(fonts[family], hasLength(5), reason: '$family: 400 to 800');
    }
  });

  test('the monospace font is monospaced', () {
    expect(_width('iiiiiiiiii', kMonoFontFamily),
        _width('MMMMMMMMMM', kMonoFontFamily));
  });

  // A family named by a string can be one no platform bundles ('monospace' on
  // iOS, 'Georgia' on Android and the web), which falls back silently. So
  // lib/ names families only through the constants, which the test above
  // ties to pubspec.yaml (review R1-3).
  test('nothing in lib/ names a font family by a string', () {
    final literal =
        RegExp(r'''fontFamily(Fallback)?:\s*(const\s*)?['"\[]''');
    final offenders = <String>[];
    for (final file in Directory('lib').listSync(recursive: true)) {
      if (file is! File || !file.path.endsWith('.dart')) continue;
      final lines = file.readAsLinesSync();
      for (var i = 0; i < lines.length; i++) {
        final line = lines[i];
        if (literal.hasMatch(line) || line.contains('package:google_fonts/')) {
          offenders.add('${file.path}:${i + 1}: ${line.trim()}');
        }
      }
    }
    expect(offenders, isEmpty,
        reason: 'use kFontFamily / kMonoFontFamily (design_tokens.dart)');
  });
}
