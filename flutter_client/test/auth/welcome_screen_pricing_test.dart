// The marketing page's hosted card must not promise what the service does not
// provide, nor quote a price nobody charges (issue #432).
//
// It listed "Daily backups", "Priority support", "Share links with custom
// domains" and "Background Strava sync", none of which exists, and quoted a
// hard-coded "€4 / month" that matched no plan. The price now comes from the
// server's catalogue, and only from a plan the server actually sells.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/auth/welcome_screen.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';

PlanInfo _plan(String id, String price, {bool purchasable = true}) => PlanInfo(
    id: id, name: id, priceLabel: price, features: const [],
    purchasable: purchasable);

/// A server that sells: prices nobody would hard-code, so a literal in the
/// widget cannot pass for one read from the catalogue.
Future<List<PlanInfo>> _selling() async => [
      _plan('free', 'Free', purchasable: false),
      _plan('tier_1', '€7.77 / month'),
      _plan('tier_2', '€8.88 / month'),
    ];

/// A self-hosted server: it still lists the default catalogue, prices and all,
/// but sells none of it.
Future<List<PlanInfo>> _selfHosted() async => [
      _plan('free', 'Free', purchasable: false),
      _plan('tier_1', '€0.99 / month', purchasable: false),
    ];

Future<List<PlanInfo>> _failing() async => throw Exception('offline');

Future<void> _pumpWelcome(
    WidgetTester tester, Future<List<PlanInfo>> Function() loadPlans) async {
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

  await tester.pumpWidget(MaterialApp(home: WelcomeScreen(loadPlans: loadPlans)));
  await tester.pump(); // resolve the catalogue future
  await tester.pump(); // rebuild with it
}

/// Every string rendered under [scope], lower-cased so a claim cannot slip
/// through by changing case.
List<String> _texts(WidgetTester tester, Finder scope) => tester
    .widgetList<RichText>(find.descendant(of: scope, matching: find.byType(RichText)))
    .map((r) => r.text.toPlainText().toLowerCase())
    .toList();

/// The hosted card: the nearest Stack around its "Cloud" title.
Finder _cloudCard() =>
    find.ancestor(of: find.text('Cloud'), matching: find.byType(Stack)).first;

void main() {
  testWidgets('the hosted card is rendered', (tester) async {
    await _pumpWelcome(tester, _selling);

    // Guard the guard: the checks below mean nothing if the card is absent.
    expect(find.text('Cloud'), findsOneWidget);
    expect(find.text('Same app, hosted for you'), findsOneWidget);
  });

  testWidgets('the page promises no feature that does not exist',
      (tester) async {
    await _pumpWelcome(tester, _selling);

    final texts = _texts(tester, find.byType(WelcomeScreen));
    expect(texts, isNotEmpty);
    for (final claim in [
      'backup',
      'priority support',
      'custom domain',
      'background strava sync',
      // Hosted plans have limits self-hosting does not.
      'everything in self-hosted',
    ]) {
      expect(texts.where((t) => t.contains(claim)), isEmpty,
          reason: '"$claim" is advertised but not provided');
    }
  });

  testWidgets('the hosted price is the cheapest plan the server sells',
      (tester) async {
    await _pumpWelcome(tester, _selling);

    expect(find.descendant(of: _cloudCard(), matching: find.text('€7.77 / month')),
        findsOneWidget);
    // Only one price, and no other number dressed up as one.
    expect(_texts(tester, _cloudCard()).where((t) => t.contains('€')),
        ['€7.77 / month']);
  });

  testWidgets('a server that sells nothing gets no price at all',
      (tester) async {
    await _pumpWelcome(tester, _selfHosted);

    expect(_texts(tester, _cloudCard()).where((t) => t.contains('€')), isEmpty);
    expect(find.text('Cloud'), findsOneWidget);
  });

  testWidgets('an unreachable catalogue gets no price either', (tester) async {
    await _pumpWelcome(tester, _failing);

    expect(_texts(tester, _cloudCard()).where((t) => t.contains('€')), isEmpty);
    expect(find.text('Cloud'), findsOneWidget);
  });

  group('hostedPriceLabel', () {
    test('skips plans that are not for sale', () async {
      expect(hostedPriceLabel(await _selling()), '€7.77 / month');
    });

    test('is null when nothing is for sale', () async {
      expect(hostedPriceLabel(await _selfHosted()), isNull);
      expect(hostedPriceLabel(const []), isNull);
    });
  });
}
