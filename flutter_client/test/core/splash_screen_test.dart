// Tests for the brand splash and the gate that shows it
// (lib/src/core/splash_screen.dart).
//
// Two things matter here. First, the layout switch: the design has a stacked
// board and a side-by-side one, and only the wide board carries the blurb, the
// rule and the "Loading" label.
//
// Second, and the reason AuthNotifier gained isRestoring: the splash must cover
// the app while a persisted session is restored at start-up, and must *not*
// come back over the login form during an interactive sign-in. Both states used
// to be the single isLoading flag.

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/app_version.dart';
import 'package:traxjourney_client/src/core/brand_mark.dart';
import 'package:traxjourney_client/src/core/splash_screen.dart';

/// Lets a test drive the two loading flags independently, which the real
/// notifier only ever does from inside a network call.
class _FakeAuth extends AuthNotifier {
  _FakeAuth() : super(AuthService());

  bool restoring = false;
  bool signingIn = false;

  @override
  bool get isRestoring => restoring;

  @override
  bool get isLoading => signingIn;

  void publish() => notifyListeners();
}

Future<void> _pumpSplash(WidgetTester tester, Size size) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(const MaterialApp(home: SplashScreen()));
  await tester.pump();
}

/// Mounts the splash the way main.dart does — from `MaterialApp.builder`, above
/// the Navigator and outside any Material. That placement is what puts the
/// error DefaultTextStyle in scope, so the decoration test must reproduce it.
Future<void> _pumpSplashFromAppBuilder(WidgetTester tester, Size size) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(
    MaterialApp(
      builder: (context, child) => const SplashScreen(),
      home: const SizedBox.shrink(),
    ),
  );
  await tester.pump();
}

/// The style a [Text] actually paints with, after merging in the inherited
/// DefaultTextStyle — which is where the stray decoration came from.
TextStyle _paintedStyle(WidgetTester tester, Finder text) =>
    (tester.renderObject(text) as RenderParagraph).text.style!;

Future<void> _pumpGate(WidgetTester tester, _FakeAuth auth) async {
  tester.view.physicalSize = const Size(400, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(
    ChangeNotifierProvider<AuthNotifier>.value(
      value: auth,
      child: const MaterialApp(
        home: SplashGate(child: Text('the app')),
      ),
    ),
  );
  await tester.pump();
}

void main() {
  group('SplashScreen layout', () {
    testWidgets('below the breakpoint it stacks, with no wide-only pieces',
        (tester) async {
      await _pumpSplash(tester, const Size(390, 844));

      expect(find.byType(BrandMark), findsOneWidget);
      expect(find.text('TraxJourney'), findsOneWidget);
      expect(find.text('MERGE · VISUALISE · EXPORT'), findsOneWidget);
      // The blurb and the "Loading" label belong to the large board only.
      expect(find.textContaining('one continuous journey'), findsNothing);
      expect(find.text('Loading'), findsNothing);
    });

    testWidgets('at the breakpoint it switches to the wide board',
        (tester) async {
      await _pumpSplash(tester, const Size(kSplashWideBreakpoint, 900));

      expect(find.byType(BrandMark), findsOneWidget);
      expect(find.text('TraxJourney'), findsOneWidget);
      expect(find.textContaining('one continuous journey'), findsOneWidget);
      expect(find.text('Loading'), findsOneWidget);
    });

    testWidgets('one pixel under the breakpoint is still the stacked board',
        (tester) async {
      await _pumpSplash(tester, const Size(kSplashWideBreakpoint - 1, 900));

      expect(find.text('Loading'), findsNothing);
    });

    testWidgets('the mark grows on the wide board', (tester) async {
      await _pumpSplash(tester, const Size(390, 844));
      final stacked = tester.getSize(find.byType(BrandMark));

      await _pumpSplash(tester, const Size(1200, 900));
      final wide = tester.getSize(find.byType(BrandMark));

      expect(wide.width, greaterThan(stacked.width));
    });

    testWidgets('names the app version alone until the server one is known',
        (tester) async {
      await _pumpSplash(tester, const Size(390, 844));

      // The splash omits the clause rather than saying `server unknown`: it
      // shows for a few hundred milliseconds, long enough for /api/version to
      // land mid-display, so stating the absence would visibly flip while the
      // user watches. Every other surface is a diagnostic readout and does say
      // `unknown` — see app_version_test.dart.
      expect(find.text('app dev'), findsOneWidget);
      expect(find.textContaining('server'), findsNothing);
    });

    // The splash is up before any request can have come back, and stays up on
    // a cold offline start — so "unknown" is the state it must render first,
    // and it must fill itself in without a rebuild of the screen (#275).
    testWidgets('fills the server half in when the check answers',
        (tester) async {
      addTearDown(() => serverVersion.value = null);
      await _pumpSplash(tester, const Size(390, 844));
      expect(find.text('app dev'), findsOneWidget);

      serverVersion.value = 'v9.9.9';
      await tester.pump();

      expect(find.text('app dev · server v9.9.9'), findsOneWidget);
    });

    testWidgets('the longer label still fits a narrow phone', (tester) async {
      addTearDown(() => serverVersion.value = null);
      serverVersion.value = 'validation-0a1b2c3';
      await _pumpSplash(tester, const Size(320, 568));

      expect(tester.takeException(), isNull);
    });

    testWidgets('lays out without overflow on a short, wide window',
        (tester) async {
      await _pumpSplash(tester, const Size(1000, 420));

      expect(tester.takeException(), isNull);
    });
  });

  // #177: the splash is the one screen with no Material above it, so it
  // inherited MaterialApp's fallback DefaultTextStyle — red monospace with a
  // yellow double underline, meant to nag you into adding a Material. The type
  // styles override colour and metrics but not decoration, so the underline
  // came through on every label.
  group('SplashScreen type', () {
    testWidgets('the stacked board carries no inherited text decoration',
        (tester) async {
      await _pumpSplashFromAppBuilder(tester, const Size(390, 844));

      for (final label in [
        find.text('TraxJourney'),
        find.text('MERGE · VISUALISE · EXPORT'),
        find.text('app dev'),
      ]) {
        expect(_paintedStyle(tester, label).decoration, TextDecoration.none,
            reason: '${(label.evaluate().single.widget as Text).data} '
                'inherited a decoration');
      }
    });

    testWidgets('the wide board carries no inherited text decoration',
        (tester) async {
      await _pumpSplashFromAppBuilder(tester, const Size(1200, 900));

      for (final label in [
        find.text('TraxJourney'),
        find.textContaining('one continuous journey'),
        find.text('Loading'),
      ]) {
        expect(_paintedStyle(tester, label).decoration, TextDecoration.none);
      }
    });
  });

  // The pre-Flutter boot chrome and the Dart splash both pick their board off a
  // 760px breakpoint, so both need the page to report the real device width.
  // Without the viewport meta a phone lays out at 980px and gets the wide board
  // painted at twice the screen width before the shrink-to-fit lands (#177).
  group('web boot chrome', () {
    test('index.html pins the viewport to the device width', () {
      // `flutter test` runs from the package root.
      final html = File('web/index.html').readAsStringSync();

      expect(
        html,
        contains('<meta name="viewport" content="width=device-width'),
        reason: 'web/index.html must declare a device-width viewport',
      );
    });
  });

  group('SplashGate', () {
    testWidgets('covers the app while a session is being restored',
        (tester) async {
      final auth = _FakeAuth()..restoring = true;
      await _pumpGate(tester, auth);

      expect(find.byType(SplashScreen), findsOneWidget);
    });

    testWidgets('uncovers the app once the restore finishes', (tester) async {
      final auth = _FakeAuth()..restoring = true;
      await _pumpGate(tester, auth);
      expect(find.byType(SplashScreen), findsOneWidget);

      auth
        ..restoring = false
        ..publish();
      await tester.pump();

      expect(find.byType(SplashScreen), findsNothing);
      expect(find.text('the app'), findsOneWidget);
    });

    testWidgets('does not reappear for an interactive sign-in', (tester) async {
      // The regression isRestoring exists to prevent: loginWithPassword also
      // raises isLoading, and keying the splash off that flag would throw it
      // back over the login form on every attempt.
      final auth = _FakeAuth()
        ..restoring = false
        ..signingIn = true;
      await _pumpGate(tester, auth);

      expect(find.byType(SplashScreen), findsNothing);
      expect(find.text('the app'), findsOneWidget);
    });
  });
}
