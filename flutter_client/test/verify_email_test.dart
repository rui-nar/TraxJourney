// Widget tests for email verification (issue #110): the /verify-email/{token}
// landing screen and the "confirm your email" banner.
//
// The screen must work signed in *and* signed out — the recipient clicks the
// link from their inbox, which may be a browser with no session — so both
// paths are covered here.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/verify_email_banner.dart';
import 'package:traxjourney_client/src/auth/verify_email_screen.dart';

class _FakeAuthService extends AuthService {
  ApiException? verifyError;
  ApiException? resendError;
  int verifyCalls = 0;
  int resendCalls = 0;
  Map<String, dynamic> profile = const {};

  @override
  Future<void> verifyEmail(String token) async {
    verifyCalls++;
    final err = verifyError;
    if (err != null) throw err;
  }

  @override
  Future<void> resendVerification() async {
    resendCalls++;
    final err = resendError;
    if (err != null) throw err;
  }

  @override
  Future<Map<String, dynamic>> getMe() async => profile;
}

/// An AuthNotifier with a user already set, bypassing the login round-trip.
class _FakeAuth extends AuthNotifier {
  User? _override;

  _FakeAuth(super.service, this._override);

  @override
  User? get user => _override;

  void setUser(User? u) {
    _override = u;
    notifyListeners();
  }
}

Widget _wrap(AuthNotifier auth, Widget child) => ChangeNotifierProvider<
        AuthNotifier>.value(
    value: auth, child: MaterialApp(home: child));

const _unverified = User(
  id: '1',
  email: 'alice@example.com',
  displayName: 'Alice Smith',
  avatarUrl: '',
  authProvider: 'local',
);

void main() {
  setUp(() => VerifyEmailBanner.dismissed = false);

  group('VerifyEmailScreen', () {
    testWidgets('consumes the token and confirms on success', (tester) async {
      final svc = _FakeAuthService();
      final auth = _FakeAuth(svc, _unverified);

      await tester.pumpWidget(_wrap(auth, const VerifyEmailScreen(token: 't1')));
      await tester.pumpAndSettle();

      expect(svc.verifyCalls, 1);
      expect(find.text('Email confirmed'), findsOneWidget);
    });

    testWidgets('shows the server message when the link is dead',
        (tester) async {
      final svc = _FakeAuthService()
        ..verifyError = ApiException(404,
            '{"detail":"This verification link is invalid or has expired."}');
      final auth = _FakeAuth(svc, _unverified);

      await tester.pumpWidget(_wrap(auth, const VerifyEmailScreen(token: 't1')));
      await tester.pumpAndSettle();

      expect(find.text('Could not confirm'), findsOneWidget);
      expect(
          find.text('This verification link is invalid or has expired.'),
          findsOneWidget);
    });

    testWidgets('offers sign-in rather than trips when signed out',
        (tester) async {
      final svc = _FakeAuthService();
      final auth = _FakeAuth(svc, null);

      await tester.pumpWidget(_wrap(auth, const VerifyEmailScreen(token: 't1')));
      await tester.pumpAndSettle();

      expect(find.text('Email confirmed'), findsOneWidget);
      expect(find.text('Sign in'), findsOneWidget);
      expect(find.text('Go to my trips'), findsNothing);
    });
  });

  group('VerifyEmailBanner', () {
    testWidgets('prompts an unverified account', (tester) async {
      final auth = _FakeAuth(_FakeAuthService(), _unverified);
      await tester.pumpWidget(_wrap(auth, const Scaffold(
          body: VerifyEmailBanner())));
      await tester.pump();

      expect(find.textContaining('alice@example.com'), findsOneWidget);
      expect(find.text('Resend'), findsOneWidget);
    });

    testWidgets('hidden once verified', (tester) async {
      final auth = _FakeAuth(
          _FakeAuthService(),
          const User(
            id: '1',
            email: 'alice@example.com',
            displayName: 'Alice',
            avatarUrl: '',
            authProvider: 'local',
            emailVerified: true,
          ));
      await tester.pumpWidget(_wrap(auth, const Scaffold(
          body: VerifyEmailBanner())));
      await tester.pump();

      expect(find.text('Resend'), findsNothing);
    });

    testWidgets('hidden for an account with no address at all', (tester) async {
      // The seeded admin. Prompting someone to confirm an address they do not
      // have would be nonsense.
      final auth = _FakeAuth(
          _FakeAuthService(),
          const User(
            id: '1',
            email: '',
            displayName: 'admin',
            avatarUrl: '',
            authProvider: 'local',
          ));
      await tester.pumpWidget(_wrap(auth, const Scaffold(
          body: VerifyEmailBanner())));
      await tester.pump();

      expect(find.text('Resend'), findsNothing);
    });

    testWidgets('resend reports success', (tester) async {
      final svc = _FakeAuthService();
      final auth = _FakeAuth(svc, _unverified);
      await tester.pumpWidget(_wrap(auth, const Scaffold(
          body: VerifyEmailBanner())));
      await tester.pump();

      await tester.tap(find.text('Resend'));
      await tester.pumpAndSettle();

      expect(svc.resendCalls, 1);
      expect(find.text('Sent — check your inbox.'), findsOneWidget);
    });

    testWidgets('resend surfaces a 429 instead of claiming success',
        (tester) async {
      final svc = _FakeAuthService()
        ..resendError = ApiException(
            429, '{"detail":"Too many verification emails requested."}');
      final auth = _FakeAuth(svc, _unverified);
      await tester.pumpWidget(_wrap(auth, const Scaffold(
          body: VerifyEmailBanner())));
      await tester.pump();

      await tester.tap(find.text('Resend'));
      await tester.pumpAndSettle();

      expect(find.text('Too many verification emails requested.'),
          findsOneWidget);
    });

    testWidgets('dismiss hides it', (tester) async {
      final auth = _FakeAuth(_FakeAuthService(), _unverified);
      await tester.pumpWidget(_wrap(auth, const Scaffold(
          body: VerifyEmailBanner())));
      await tester.pump();

      await tester.tap(find.byTooltip('Dismiss'));
      await tester.pumpAndSettle();

      expect(find.text('Resend'), findsNothing);
    });
  });
}
