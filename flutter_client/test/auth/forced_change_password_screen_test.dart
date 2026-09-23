import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/forced_change_password_screen.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';

class _FakeSettingsService extends SettingsService {
  Object? changePasswordError;
  bool called = false;

  @override
  Future<({String? token, Map<String, dynamic> user})> changePassword({
    required String current,
    required String next,
  }) async {
    called = true;
    if (changePasswordError != null) throw changePasswordError!;
    return (
      token: 'new-jwt-token',
      user: <String, dynamic>{
        'id': '1', 'email': 'a@x.com', 'display_name': 'A',
        'auth_provider': 'local', 'password_change_required': false,
      },
    );
  }
}

Future<void> _pump(WidgetTester tester, SettingsService service, AuthNotifier auth) async {
  await tester.pumpWidget(MaterialApp(
    home: ChangeNotifierProvider<AuthNotifier>.value(
      value: auth,
      child: ForcedChangePasswordScreen(service: service),
    ),
  ));
  await tester.pumpAndSettle();
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('blocks submit with empty fields, no service call', (tester) async {
    final svc = _FakeSettingsService();
    final auth = AuthNotifier(AuthService());
    await _pump(tester, svc, auth);

    await tester.tap(find.widgetWithText(FilledButton, 'Change password'));
    await tester.pumpAndSettle();

    expect(find.text('Fill in all fields.'), findsOneWidget);
    expect(svc.called, isFalse);
  });

  testWidgets('blocks submit when new/confirm mismatch, no service call',
      (tester) async {
    final svc = _FakeSettingsService();
    final auth = AuthNotifier(AuthService());
    await _pump(tester, svc, auth);

    await tester.enterText(find.widgetWithText(TextField, 'Current password'), 'old-pw');
    await tester.enterText(find.widgetWithText(TextField, 'New password'), 'new-pw-1');
    await tester.enterText(find.widgetWithText(TextField, 'Confirm new password'), 'new-pw-2');
    await tester.tap(find.widgetWithText(FilledButton, 'Change password'));
    await tester.pumpAndSettle();

    expect(find.text('New passwords do not match.'), findsOneWidget);
    expect(svc.called, isFalse);
  });

  testWidgets('surfaces the server error (e.g. wrong current password)',
      (tester) async {
    final svc = _FakeSettingsService()
      ..changePasswordError = Exception('Current password is incorrect');
    final auth = AuthNotifier(AuthService());
    await _pump(tester, svc, auth);

    await tester.enterText(find.widgetWithText(TextField, 'Current password'), 'wrong-pw');
    await tester.enterText(find.widgetWithText(TextField, 'New password'), 'new-pw-1');
    await tester.enterText(find.widgetWithText(TextField, 'Confirm new password'), 'new-pw-1');
    await tester.tap(find.widgetWithText(FilledButton, 'Change password'));
    await tester.pumpAndSettle();

    expect(find.text('Current password is incorrect'), findsOneWidget);
    expect(svc.called, isTrue);
  });

  testWidgets(
      'success updates AuthNotifier.user from the response, not a stale-token refetch (issue #67)',
      (tester) async {
    final svc = _FakeSettingsService();
    final auth = AuthNotifier(AuthService());
    await _pump(tester, svc, auth);

    await tester.enterText(find.widgetWithText(TextField, 'Current password'), 'old-pw');
    await tester.enterText(find.widgetWithText(TextField, 'New password'), 'new-pw-1');
    await tester.enterText(find.widgetWithText(TextField, 'Confirm new password'), 'new-pw-1');
    await tester.tap(find.widgetWithText(FilledButton, 'Change password'));
    await tester.pumpAndSettle();

    expect(svc.called, isTrue);
    expect(auth.user, isNotNull);
    expect(auth.user!.passwordChangeRequired, isFalse);
    expect(auth.user!.email, 'a@x.com');
  });
}
