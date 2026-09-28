/// The privacy policy and terms links point at the server the app talks to
/// (issue #421), so a self-hosted server's users read that server's pages.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/legal_links.dart';

import '../helpers/fake_url_launcher.dart';

void main() {
  group('legalPageUri', () {
    test('uses the server address when the app has one (Android, iOS)', () {
      expect(
        legalPageUri(kPrivacyPolicyPath, apiBaseUrl: 'https://trax.example.com'),
        Uri.parse('https://trax.example.com/privacy'),
      );
    });

    test('uses the page origin when the web app is served by the server', () {
      expect(
        legalPageUri(kTermsOfServicePath,
            apiBaseUrl: '',
            pageBase: Uri.parse('https://traxjourney.com/projects/Trip?x=1')),
        Uri.parse('https://traxjourney.com/terms'),
      );
    });

    test('defaults to the global api client', () {
      final previous = api;
      addTearDown(() => api = previous);
      api = ApiClient(baseUrl: 'https://self.example.org');

      expect(legalPageUri(kTermsOfServicePath),
          Uri.parse('https://self.example.org/terms'));
    });
  });

  testWidgets('LegalNotice links both pages', (tester) async {
    final previous = api;
    addTearDown(() => api = previous);
    api = ApiClient(baseUrl: 'https://trax.example.com');
    final launcher = installFakeUrlLauncher();

    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(body: LegalNotice(action: 'continuing')),
    ));
    await tester.tap(find.widgetWithText(TextButton, 'Terms of Service'));
    await tester.tap(find.widgetWithText(TextButton, 'Privacy Policy'));
    await tester.pump();

    expect(launcher.launched, [
      'https://trax.example.com/terms',
      'https://trax.example.com/privacy',
    ]);
    expect(find.textContaining('By continuing you agree to the'), findsOneWidget);
  });
}
