// The marketing page's footer names both versions (issue #275).
//
// It used to be a Row of two unflexible labels either side of a Spacer, which
// is the one layout that cannot give: the labels already ran off the side of
// the phone footer, and naming both versions makes the left one half as long
// again. The page is laid out in the real fonts (test/helpers/real_fonts.dart),
// so an overflow anywhere on it fails the test as well.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/theme.dart';
import 'package:traxjourney_client/src/auth/welcome_screen.dart';
import 'package:traxjourney_client/src/core/app_version.dart';

import '../helpers/real_fonts.dart';

/// The version footer is the only label on the page carrying "server".
final _footerVersion = find.textContaining('· server ');

Future<void> _pumpWelcome(WidgetTester tester, Size size) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(() => serverVersion.value = null);

  await tester.pumpWidget(
      MaterialApp(theme: lightTheme, home: const WelcomeScreen()));
  await tester.pump();
}

void main() {
  setUpAll(loadRealFonts);

  testWidgets('the footer names both versions', (tester) async {
    serverVersion.value = 'v9.9.9';
    await _pumpWelcome(tester, const Size(1280, 900));

    expect(find.textContaining('app dev · server v9.9.9'), findsOneWidget);
  });

  testWidgets('the footer version fits the width of a phone', (tester) async {
    serverVersion.value = 'validation-0a1b2c3';
    await _pumpWelcome(tester, const Size(390, 844));

    expect(tester.getRect(_footerVersion).right, lessThanOrEqualTo(390.0));
  });

  testWidgets('and the width of a desktop window', (tester) async {
    serverVersion.value = 'validation-0a1b2c3';
    await _pumpWelcome(tester, const Size(1280, 900));

    expect(tester.getRect(_footerVersion).right, lessThanOrEqualTo(1280.0));
  });
}
