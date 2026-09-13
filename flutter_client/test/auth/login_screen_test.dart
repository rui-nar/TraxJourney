// Widget tests for ServerConfigDialog and SelfHostingLink (login_screen.dart's
// "Self-hosting?" affordance): prefill from a saved override, validation,
// save/cancel round-trip through core/server_config.dart, and that the
// configured server URL stays visible on the login screen rather than only
// inside the dialog. A separate group below pumps LoginScreen itself.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/login_screen.dart';
import 'package:traxjourney_client/src/core/app_version.dart';
import 'package:traxjourney_client/src/core/server_config.dart';

/// Mutable box so a test can read the dialog's pop() result after tapping
/// Save/Cancel — showDialog()'s own return value isn't available until its
/// awaiting future resolves, which happens inside the pumped widget tree.
class _Result {
  bool? value;
}

Future<_Result> _pump(WidgetTester tester) async {
  final result = _Result();
  await tester.pumpWidget(MaterialApp(
    home: Builder(
      builder: (context) => ElevatedButton(
        onPressed: () async {
          result.value = await showDialog<bool>(
            context: context,
            builder: (_) => const ServerConfigDialog(),
          );
        },
        child: const Text('open'),
      ),
    ),
  ));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  return result;
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('opens blank when no override is saved', (tester) async {
    await _pump(tester);

    final field = find.widgetWithText(TextField, 'Server URL');
    expect(field, findsOneWidget);
    expect(tester.widget<TextField>(field).controller!.text, isEmpty);
  });

  testWidgets('prefills the field with a previously saved override',
      (tester) async {
    await writeCustomServerUrl('https://home.example.com');

    await _pump(tester);

    expect(find.text('https://home.example.com'), findsOneWidget);
  });

  testWidgets('rejects a malformed URL without saving', (tester) async {
    await _pump(tester);

    await tester.enterText(
        find.widgetWithText(TextField, 'Server URL'), 'not-a-url');
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Enter a full URL'), findsOneWidget);
    expect(await readCustomServerUrl(), isNull);
  });

  testWidgets('saves a valid URL and pops true', (tester) async {
    final result = await _pump(tester);

    await tester.enterText(
        find.widgetWithText(TextField, 'Server URL'), 'https://trax.example.com');
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(result.value, isTrue);
    expect(await readCustomServerUrl(), 'https://trax.example.com');
  });

  testWidgets('Cancel pops false without saving', (tester) async {
    final result = await _pump(tester);

    await tester.enterText(
        find.widgetWithText(TextField, 'Server URL'), 'https://trax.example.com');
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();

    expect(result.value, isFalse);
    expect(await readCustomServerUrl(), isNull);
  });

  testWidgets('clearing the field back to empty removes a saved override',
      (tester) async {
    await writeCustomServerUrl('https://home.example.com');
    await _pump(tester);

    await tester.enterText(find.widgetWithText(TextField, 'Server URL'), '');
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(await readCustomServerUrl(), isNull);
  });

  group('SelfHostingLink', () {
    testWidgets('shows the plain link when no server override is saved',
        (tester) async {
      await tester.pumpWidget(
          const MaterialApp(home: Scaffold(body: SelfHostingLink())));
      await tester.pumpAndSettle();

      expect(find.text('Self-hosting? Click here'), findsOneWidget);
    });

    testWidgets('always shows the configured server URL on the login screen',
        (tester) async {
      await writeCustomServerUrl('https://trax.example.com');

      await tester.pumpWidget(
          const MaterialApp(home: Scaffold(body: SelfHostingLink())));
      await tester.pumpAndSettle();

      expect(find.textContaining('https://trax.example.com'), findsOneWidget);
      expect(find.text('Self-hosting? Click here'), findsNothing);
    });

    testWidgets('tapping the URL reopens the dialog to change it',
        (tester) async {
      await writeCustomServerUrl('https://trax.example.com');

      await tester.pumpWidget(
          const MaterialApp(home: Scaffold(body: SelfHostingLink())));
      await tester.pumpAndSettle();

      await tester.tap(find.textContaining('https://trax.example.com'));
      await tester.pumpAndSettle();

      final field = find.widgetWithText(TextField, 'Server URL');
      expect(field, findsOneWidget);
      expect(tester.widget<TextField>(field).controller!.text,
          'https://trax.example.com');

      await tester.enterText(field, 'https://new.example.com');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(find.textContaining('https://new.example.com'), findsOneWidget);
    });
  });

  group('LoginScreen', () {
    // Regression test: initState() called GoogleSignIn.instance's lightweight-
    // authentication attempt unconditionally, on the assumption that main()
    // already called GoogleSignIn.instance.initialize() (real app startup). A
    // widget test never runs main(), so the platform interface has no
    // implementation registered and the call threw synchronously, crashing
    // LoginScreen's first build — this is what app_router_redirect_test.dart's
    // "native mobile: WelcomeScreen never mounts" test hit once its redirect
    // reached /login. Also covers the "Don't have an account?" row, which
    // overflowed at the login card's 420px max width once it actually got a
    // layout pass.
    testWidgets(
        'mounts without throwing even though GoogleSignIn was never initialized',
        (tester) async {
      await tester.pumpWidget(
        ChangeNotifierProvider<AuthNotifier>(
          create: (_) => AuthNotifier(AuthService()),
          child: const MaterialApp(home: LoginScreen()),
        ),
      );
      await tester.pumpAndSettle();

      expect(tester.takeException(), isNull);
      expect(find.byType(LoginScreen), findsOneWidget);
    });

    // Issue #275: the card's footer names both versions. The mount test above
    // is what guards the longer label against re-overflowing the 420px card.
    testWidgets('the footer names both the app and the server version',
        (tester) async {
      addTearDown(() => serverVersion.value = null);
      serverVersion.value = 'v9.9.9';

      await tester.pumpWidget(
        ChangeNotifierProvider<AuthNotifier>(
          create: (_) => AuthNotifier(AuthService()),
          child: const MaterialApp(home: LoginScreen()),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('app dev · server v9.9.9'), findsOneWidget);
    });
  });
}
