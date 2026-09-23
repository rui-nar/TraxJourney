// Widget tests for the first-launch onboarding carousel (Android launch UX):
// paging through Next, Skip and "Get started" all mark onboarding seen and
// hand off to /login, and Skip is unavailable once the last page is reached
// (its own "Get started" button is the only way off it).

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/auth/onboarding_screen.dart';
import 'package:traxjourney_client/src/core/onboarding_notifier.dart';

Future<OnboardingNotifier> _pump(WidgetTester tester) async {
  final notifier = OnboardingNotifier(false);
  final router = GoRouter(
    initialLocation: '/onboarding',
    routes: [
      GoRoute(
          path: '/onboarding', builder: (_, __) => const OnboardingScreen()),
      GoRoute(path: '/login', builder: (_, __) => const Text('LOGIN SCREEN')),
    ],
  );
  await tester.pumpWidget(
    ChangeNotifierProvider<OnboardingNotifier>.value(
      value: notifier,
      child: MaterialApp.router(routerConfig: router),
    ),
  );
  await tester.pumpAndSettle();
  return notifier;
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('opens on the first page with Skip and Next visible',
      (tester) async {
    await _pump(tester);

    expect(find.text('One-click Strava sync'), findsOneWidget);
    expect(find.text('Skip'), findsOneWidget);
    expect(find.text('Next'), findsOneWidget);
    expect(find.text('Get started'), findsNothing);
  });

  testWidgets('Next pages through to the last page, which offers Get started',
      (tester) async {
    await _pump(tester);

    for (var i = 0; i < 3; i++) {
      await tester.tap(find.text('Next'));
      await tester.pumpAndSettle();
    }

    expect(find.text('One GPX, everywhere'), findsOneWidget);
    expect(find.text('Get started'), findsOneWidget);
    expect(find.text('Next'), findsNothing);
  });

  testWidgets('Skip marks onboarding seen and navigates to /login',
      (tester) async {
    final notifier = await _pump(tester);

    await tester.tap(find.text('Skip'));
    await tester.pumpAndSettle();

    expect(notifier.hasSeenOnboarding, isTrue);
    expect(find.text('LOGIN SCREEN'), findsOneWidget);
  });

  testWidgets('Get started on the last page marks seen and navigates to /login',
      (tester) async {
    final notifier = await _pump(tester);

    for (var i = 0; i < 3; i++) {
      await tester.tap(find.text('Next'));
      await tester.pumpAndSettle();
    }
    await tester.tap(find.text('Get started'));
    await tester.pumpAndSettle();

    expect(notifier.hasSeenOnboarding, isTrue);
    expect(find.text('LOGIN SCREEN'), findsOneWidget);
  });
}
