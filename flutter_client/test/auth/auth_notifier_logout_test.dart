/// Signing out must sign out even when a step of it fails (issue #429).
///
/// The router keeps a user it still sees away from /login, so a logout that
/// threw before clearing the user left a dead session on screen — right after
/// an account deletion, for one.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';

class _FailingAuthService extends AuthService {
  @override
  Future<void> logout() async => throw Exception('storage unavailable');
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('the user is signed out even when clearing the stored session fails',
      () async {
    final auth = AuthNotifier(_FailingAuthService())
      ..updateUser({'id': '1', 'email': 'a@example.com'});
    var notified = false;
    auth.addListener(() => notified = true);

    await expectLater(auth.logout(), throwsA(isA<Exception>()));

    expect(auth.user, isNull);
    expect(notified, isTrue, reason: 'the router never heard of the sign-out');
  });
}
