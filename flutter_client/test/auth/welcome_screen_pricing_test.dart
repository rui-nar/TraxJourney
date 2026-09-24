// The marketing page's hosted card must not promise what the service does not
// provide (issue #432).
//
// It listed "Daily backups", "Priority support", "Share links with custom
// domains" and "Background Strava sync". There are no per-user backups, no
// support tiers, no custom share domains and no background sync: every account
// shares one server-wide database copy, and Strava sync runs when you ask.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/auth/welcome_screen.dart';

Future<void> _pumpWelcome(WidgetTester tester) async {
  tester.view.physicalSize = const Size(1280, 900);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  // The page overflows under the test font (see welcome_screen_version_test);
  // that is not what this test is asking about.
  final previous = FlutterError.onError;
  FlutterError.onError = (details) {
    if (details.exceptionAsString().contains('overflowed')) return;
    previous?.call(details);
  };
  addTearDown(() => FlutterError.onError = previous);

  await tester.pumpWidget(const MaterialApp(home: WelcomeScreen()));
  await tester.pump();
}

void main() {
  testWidgets('the hosted card is rendered', (tester) async {
    await _pumpWelcome(tester);

    // Guard the guard: the checks below mean nothing if the card is absent.
    expect(find.text('Cloud'), findsOneWidget);
    expect(find.text('Everything in self-hosted'), findsOneWidget);
  });

  testWidgets('the hosted card promises no feature that does not exist',
      (tester) async {
    await _pumpWelcome(tester);

    for (final claim in [
      'backup',
      'Priority support',
      'custom domain',
      'Background Strava sync',
    ]) {
      expect(find.textContaining(claim, findRichText: true), findsNothing,
          reason: '"$claim" is advertised but not provided');
    }
  });
}
