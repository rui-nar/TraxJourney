// Tests for the shared version labels (lib/src/core/app_version.dart) and for
// VersionGate as their single writer (issue #275).
//
// The label has to survive the cases where there is no server version to show:
// the first frame beats the request home, the device is offline, the server is
// down. None of those may render a blank, a hang, or a crash — the label says
// "server unknown" and corrects itself if an answer arrives.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/app_version.dart';
import 'package:traxjourney_client/src/core/version_gate.dart';

/// The notifier is app-wide, so every test starts from "not yet known" and
/// leaves it that way.
void _resetServerVersion() {
  serverVersion.value = null;
  addTearDown(() => serverVersion.value = null);
}

/// Swaps the singleton ApiClient for one answering /api/version from [handler].
void _stubApi(Future<http.Response> Function(http.Request) handler) {
  final original = api;
  api = ApiClient(
      baseUrl: 'http://trax.example.com', httpClient: MockClient(handler));
  addTearDown(() => api = original);
}

Future<void> _pumpGate(WidgetTester tester) async {
  await tester.pumpWidget(
    const MaterialApp(
      home: VersionGate(child: Scaffold(body: VersionText())),
    ),
  );
  // One pump runs the post-frame check, one settles the response.
  await tester.pump();
  await tester.pump();
}

void main() {
  group('versionLabel', () {
    test('names both halves when the server version is known', () {
      expect(versionLabel('v0.42.0', 'v0.42.0'),
          'app v0.42.0 · server v0.42.0');
    });

    test('differing versions are shown plainly, both named', () {
      expect(versionLabel('v0.41.0', 'v0.42.0'),
          'app v0.41.0 · server v0.42.0');
    });

    test('an unknown server version says so rather than going blank', () {
      expect(versionLabel('v0.42.0', null), 'app v0.42.0 · server unknown');
      expect(versionLabel('v0.42.0', ''), 'app v0.42.0 · server unknown');
    });

    test('a local dev build reads the same way', () {
      expect(versionLabel('dev', 'dev'), 'app dev · server dev');
    });
  });

  group('VersionText', () {
    testWidgets('renders immediately, before any server answer', (tester) async {
      _resetServerVersion();

      await tester.pumpWidget(
          const MaterialApp(home: Scaffold(body: VersionText())));

      expect(find.text('app dev · server unknown'), findsOneWidget);
    });

    testWidgets('picks up the server version when it arrives', (tester) async {
      _resetServerVersion();
      await tester.pumpWidget(
          const MaterialApp(home: Scaffold(body: VersionText())));

      serverVersion.value = 'v9.9.9';
      await tester.pump();

      expect(find.text('app dev · server v9.9.9'), findsOneWidget);
    });

    testWidgets('keeps its prefix', (tester) async {
      _resetServerVersion();

      await tester.pumpWidget(const MaterialApp(
          home: Scaffold(body: VersionText(prefix: '© 2026 TraxJourney · '))));

      expect(find.text('© 2026 TraxJourney · app dev · server unknown'),
          findsOneWidget);
    });
  });

  group('VersionGate publishes the server version', () {
    testWidgets('a successful check fills the label in', (tester) async {
      _resetServerVersion();
      _stubApi((req) async {
        expect(req.url.path, '/api/version');
        return http.Response(jsonEncode({'version': 'v9.9.9'}), 200);
      });

      await _pumpGate(tester);

      expect(serverVersion.value, 'v9.9.9');
      expect(find.text('app dev · server v9.9.9'), findsOneWidget);
    });

    testWidgets('offline leaves the label at "unknown", not hanging',
        (tester) async {
      _resetServerVersion();
      _stubApi((req) async => throw const SocketExceptionStandIn());

      await _pumpGate(tester);

      expect(serverVersion.value, isNull);
      expect(find.text('app dev · server unknown'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('an unauthenticated 401 degrades the same way', (tester) async {
      _resetServerVersion();
      _stubApi((req) async => http.Response('{"detail":"Not authenticated"}', 401));

      await _pumpGate(tester);

      expect(serverVersion.value, isNull);
      expect(find.text('app dev · server unknown'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('an empty version field is not published as a version',
        (tester) async {
      _resetServerVersion();
      _stubApi((req) async => http.Response(jsonEncode({'version': ''}), 200));

      await _pumpGate(tester);

      expect(serverVersion.value, isNull);
      expect(find.text('app dev · server unknown'), findsOneWidget);
    });

    testWidgets('a late answer does not write state after dispose',
        (tester) async {
      _resetServerVersion();
      _stubApi((req) async {
        await Future<void>.delayed(const Duration(milliseconds: 50));
        return http.Response(jsonEncode({'version': 'v9.9.9'}), 200);
      });

      await tester.pumpWidget(
        const MaterialApp(home: VersionGate(child: Scaffold(body: Text('x')))),
      );
      await tester.pump();
      // Unmount before the response lands.
      await tester.pumpWidget(const MaterialApp(home: Scaffold(body: Text('y'))));
      await tester.pump(const Duration(milliseconds: 100));

      expect(tester.takeException(), isNull);
      expect(serverVersion.value, isNull);
    });
  });
  group('the splash omits an unknown server version', () {
    test('the clause is dropped, not filled with a placeholder', () {
      expect(versionLabel('v1.0.0', null, omitUnknownServer: true), 'app v1.0.0');
      expect(versionLabel('v1.0.0', '', omitUnknownServer: true), 'app v1.0.0');
    });

    test('once known it reads exactly as everywhere else', () {
      expect(versionLabel('v1.0.0', 'v1.0.1', omitUnknownServer: true),
          versionLabel('v1.0.0', 'v1.0.1'));
    });

    test('every other surface still says unknown', () {
      expect(versionLabel('v1.0.0', null), 'app v1.0.0 · server unknown');
    });

    testWidgets('VersionText drops the clause, then shows it on arrival',
        (tester) async {
      serverVersion.value = null;
      await tester.pumpWidget(const MaterialApp(
          home: Scaffold(body: VersionText(omitUnknownServer: true))));
      expect(find.textContaining('server'), findsNothing);
      serverVersion.value = 'v9.9.9';
      await tester.pump();
      expect(find.textContaining('server v9.9.9'), findsOneWidget);
      serverVersion.value = null;
    });
  });
}

/// A stand-in for a dead network: MockClient only needs the call to throw.
class SocketExceptionStandIn implements Exception {
  const SocketExceptionStandIn();
}
