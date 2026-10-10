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

Future<void> _load(String family, String file) async {
  final bytes = File('test/fonts/$file').readAsBytesSync();
  await (FontLoader(family)..addFont(Future.value(ByteData.sublistView(bytes))))
      .load();
}

/// Loads Inter and JetBrains Mono at every weight the landing page uses.
/// Call from `setUpAll`.
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

/// Sets aside google_fonts' own failed attempts to fetch the fonts above (it
/// does not know they are already loaded) and nothing else, so an overflow
/// still fails the test. Call inside the test.
void ignoreFontFetchErrors() {
  final previous = FlutterError.onError;
  FlutterError.onError = (details) {
    final text = details.exceptionAsString();
    if (text.contains('GoogleFonts') || text.contains('google_fonts') ||
        text.contains('allowRuntimeFetching')) {
      return;
    }
    previous?.call(details);
  };
  addTearDown(() => FlutterError.onError = previous);
}
