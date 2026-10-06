// Tests for the minimum-client-version gate (Decision 15 of
// docs/CLIENT_STATE_MAP_PLAN.md): the pure comparison, and the blocking screen
// (native) / non-dismissable bar (web) VersionGate shows below the minimum.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/app_version.dart';
import 'package:traxjourney_client/src/core/version_gate.dart';

import '../helpers/fake_url_launcher.dart';

void _stubApi(String version, String? minimum) {
  final original = api;
  api = ApiClient(
    baseUrl: 'http://trax.example.com',
    httpClient: MockClient((req) async => http.Response(
        jsonEncode({
          'version': version,
          if (minimum != null) 'min_client_version': minimum,
        }),
        200)),
  );
  addTearDown(() {
    api = original;
    serverVersion.value = null;
  });
}

Future<void> _pump(WidgetTester tester,
    {required String client, required bool isWeb}) async {
  await tester.pumpWidget(MaterialApp(
    home: VersionGate(
      clientVersion: client,
      isWeb: isWeb,
      child: const Scaffold(body: Text('app body')),
    ),
  ));
  await tester.pump();
  await tester.pump();
}

void main() {
  group('isBelowMinimum', () {
    test('older client is below', () {
      expect(isBelowMinimum('v0.9.0', '0.10.0'), isTrue);
      expect(isBelowMinimum('1.2.3', '1.2.4'), isTrue);
      expect(isBelowMinimum('0.99.99', '1.0.0'), isTrue);
    });

    test('comparison is numeric, not textual', () {
      expect(isBelowMinimum('0.10.0', '0.9.0'), isFalse);
      expect(isBelowMinimum('0.9.0', '0.10.0'), isTrue);
    });

    test('equal versions are not below', () {
      expect(isBelowMinimum('1.1.0', '1.1.0'), isFalse);
      expect(isBelowMinimum('v1.1.0', '1.1.0'), isFalse);
    });

    test('the default minimum 0.0.0 blocks nobody', () {
      expect(isBelowMinimum('0.0.1', '0.0.0'), isFalse);
      expect(isBelowMinimum('v0.50.0', '0.0.0'), isFalse);
      expect(isBelowMinimum('dev', '0.0.0'), isFalse);
    });

    test('dev, empty and unparsable values never block', () {
      expect(isBelowMinimum('dev', '9.9.9'), isFalse);
      expect(isBelowMinimum('', '9.9.9'), isFalse);
      expect(isBelowMinimum('v1.0.0', ''), isFalse);
      expect(isBelowMinimum('v1.0.0', 'dev'), isFalse);
      expect(isBelowMinimum('banana', '1.0.0'), isFalse);
      expect(isBelowMinimum('1.0', '2.0.0'), isFalse);
    });
  });

  group('VersionGate minimum', () {
    testWidgets('native below the minimum shows the update screen',
        (tester) async {
      final launcher = installFakeUrlLauncher();
      _stubApi('v1.2.0', '1.1.0');

      await _pump(tester, client: 'v1.0.0', isWeb: false);

      expect(find.text('Update required'), findsOneWidget);
      expect(find.text('app body'), findsNothing);

      await tester.tap(find.text('Get the update'));
      await tester.pump();
      expect(launcher.launched,
          ['https://github.com/rui-nar/TraxJourney/releases/latest']);
    });

    testWidgets('web below the minimum shows the bar over the app',
        (tester) async {
      _stubApi('v1.0.0', '1.1.0');

      await _pump(tester, client: 'v1.0.0', isWeb: true);

      expect(find.textContaining('no longer supported'), findsOneWidget);
      expect(find.text('Reload'), findsOneWidget);
      expect(find.text('app body'), findsOneWidget);
      expect(find.text('Update required'), findsNothing);
    });

    testWidgets('at the minimum shows nothing', (tester) async {
      _stubApi('v1.1.0', '1.1.0');

      await _pump(tester, client: 'v1.1.0', isWeb: false);
      expect(find.text('Update required'), findsNothing);
      expect(find.text('app body'), findsOneWidget);

      await _pump(tester, client: 'v1.1.0', isWeb: true);
      expect(find.textContaining('no longer supported'), findsNothing);
      expect(find.text('Reload'), findsNothing);
    });

    testWidgets('a server without the field blocks nobody', (tester) async {
      _stubApi('v1.2.0', null);

      await _pump(tester, client: 'v0.1.0', isWeb: false);

      expect(find.text('Update required'), findsNothing);
      expect(find.text('app body'), findsOneWidget);
    });

    testWidgets('resuming re-checks and picks up a raised minimum',
        (tester) async {
      var minimum = '0.0.0';
      final original = api;
      api = ApiClient(
        baseUrl: 'http://trax.example.com',
        httpClient: MockClient((req) async => http.Response(
            jsonEncode({'version': 'v1.2.0', 'min_client_version': minimum}),
            200)),
      );
      addTearDown(() {
        api = original;
        serverVersion.value = null;
      });

      await _pump(tester, client: 'v1.0.0', isWeb: false);
      expect(find.text('Update required'), findsNothing);

      minimum = '1.1.0';
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      await tester.pump();

      expect(find.text('Update required'), findsOneWidget);
    });
  });
}
