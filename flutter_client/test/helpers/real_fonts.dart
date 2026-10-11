/// The app's real fonts, for tests that measure where text wraps.
///
/// Widget tests normally draw every glyph a full em wide, which says nothing
/// about where real text wraps or overflows, and `flutter test` does not load
/// the fonts pubspec.yaml bundles. This loads exactly those: every family and
/// file under `flutter: fonts:`, so the layout under test is the one a reader
/// gets.
library;

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

/// Every family [loadRealFonts] registered. Text drawn in any other family
/// (bar icon fonts) is measured in the test font, which
/// welcome_screen_overflow_test.dart checks against.
final loadedFontFamilies = <String>{};

/// The `flutter: fonts:` section of pubspec.yaml, as family → asset paths.
Map<String, List<String>> bundledFonts() {
  final fonts = <String, List<String>>{};
  String? family;
  for (final line in File('pubspec.yaml').readAsLinesSync()) {
    final f = RegExp(r'^\s+- family:\s*(.+?)\s*$').firstMatch(line);
    if (f != null) family = f[1];
    final a = RegExp(r'^\s+- asset:\s*(\S+)\s*$').firstMatch(line);
    if (a != null && family != null) (fonts[family] ??= []).add(a[1]!);
  }
  return fonts;
}

/// Loads every bundled font. Call from `setUpAll`.
Future<void> loadRealFonts() async {
  for (final MapEntry(key: family, value: assets) in bundledFonts().entries) {
    final loader = FontLoader(family);
    for (final asset in assets) {
      loader.addFont(
          Future.value(ByteData.sublistView(File(asset).readAsBytesSync())));
    }
    await loader.load();
    // A family that fell back to the test font would go unnoticed: the test
    // font draws every glyph a full em wide, real fonts draw "i" far narrower.
    final probe = TextPainter(
      text: TextSpan(text: 'iiiiiiiiii',
          style: TextStyle(fontFamily: family, fontSize: 10)),
      textDirection: TextDirection.ltr,
    )..layout();
    // Ten glyphs at 10 px: 100 px in the test font, 60 in a monospace font.
    expect(probe.width, lessThan(90),
        reason: '"$family" measured like the test font');
    probe.dispose();
    loadedFontFamilies.add(family);
  }
}
