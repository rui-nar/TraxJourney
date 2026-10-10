/// The landing page's real fonts, for tests that measure where text wraps.
///
/// Widget tests normally draw every glyph a full em wide, which says nothing
/// about where real text wraps or overflows. These are the exact files
/// google_fonts downloads in the app (checked against its content hashes; OFL,
/// see test/fonts/*-LICENSE.txt), each loaded under the family name
/// google_fonts asks for, so the layout under test is the one a reader gets.
library;

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:traxjourney_client/src/core/theme.dart';

final _inter = {
  FontWeight.w400: 'Inter-Regular.ttf',
  FontWeight.w500: 'Inter-Medium.ttf',
  FontWeight.w600: 'Inter-SemiBold.ttf',
  FontWeight.w700: 'Inter-Bold.ttf',
  FontWeight.w800: 'Inter-ExtraBold.ttf',
};

final _mono = {
  FontWeight.w500: 'JetBrainsMono-Medium.ttf',
  FontWeight.w700: 'JetBrainsMono-Bold.ttf',
  FontWeight.w800: 'JetBrainsMono-ExtraBold.ttf',
};

/// Every family [loadRealFonts] registered. A page under test that draws text
/// in any other family (bar icon fonts) is measured in the test font, which
/// is what welcome_screen_overflow_test.dart checks against.
final loadedFontFamilies = <String>{};

Future<void> _load(String family, String file) async {
  final bytes = File('test/fonts/$file').readAsBytesSync();
  await (FontLoader(family)..addFont(Future.value(ByteData.sublistView(bytes))))
      .load();
  // google_fonts only prints a failed lookup, so a family that silently fell
  // back to the test font would go unnoticed: the test font draws every glyph
  // a full em wide, real fonts draw "i" far narrower (review R1-4).
  final probe = TextPainter(
    text: TextSpan(text: 'iiiiiiiiii',
        style: TextStyle(fontFamily: family, fontSize: 10)),
    textDirection: TextDirection.ltr,
  )..layout();
  // Ten glyphs at 10 px: 100 px in the test font, 60 in a monospace font.
  expect(probe.width, lessThan(90),
      reason: '$file under "$family" measured like the test font');
  probe.dispose();
  loadedFontFamilies.add(family);
}

/// Loads Inter and JetBrains Mono at every weight the landing page uses.
/// Call from `setUpAll`. google_fonts' own lookups of these families still
/// fail (runtime fetching is off) and print a line each; that is expected.
Future<void> loadRealFonts() async {
  GoogleFonts.config.allowRuntimeFetching = false;
  for (final MapEntry(key: weight, value: file) in _inter.entries) {
    await _load(GoogleFonts.inter(fontWeight: weight).fontFamily!, file);
  }
  // The app theme's default family (lightTheme / darkTheme).
  await _load(GoogleFonts.inter().fontFamily!, _inter[FontWeight.w400]!);
  for (final MapEntry(key: weight, value: file) in _mono.entries) {
    await _load(GoogleFonts.jetBrainsMono(fontWeight: weight).fontFamily!, file);
  }
  await _load('Roboto', 'Roboto-SemiBold.ttf');
}

/// [lightTheme] with its elevated buttons drawn the way the app draws them.
///
/// The theme's button text style names no family, so the app draws button
/// labels in the platform's default font (Roboto on Android and the web),
/// while a widget test would use the test font, far wider. Until issue #594
/// gives the buttons Inter, this names Roboto (loaded above) explicitly.
ThemeData landingTheme() {
  final style = lightTheme.elevatedButtonTheme.style!;
  final text = style.textStyle!.resolve(const {})!;
  return lightTheme.copyWith(
    elevatedButtonTheme: ElevatedButtonThemeData(
      style: style.copyWith(
          textStyle: WidgetStatePropertyAll(text.copyWith(fontFamily: 'Roboto'))),
    ),
  );
}

