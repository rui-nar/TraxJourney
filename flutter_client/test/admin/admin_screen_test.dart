import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/admin/admin_screen.dart';
import 'package:traxjourney_client/src/admin/admin_service.dart';
import 'package:traxjourney_client/src/core/design_tokens.dart';

class _FakeAdminService extends AdminService {
  List<Map<String, dynamic>> searchResult = [];
  int? resetCalledFor;
  int? adminToggledFor;
  bool? adminToggledTo;
  Object? setAdminError;
  int? deleteCalledFor;
  Object? deleteError;
  String? emailSentSubject;
  String? emailSentBody;
  List<int>? emailSentUserIds;
  bool? emailSentToAll;
  Object? broadcastEmailError;

  Map<String, dynamic> logLevelState = {
    'effective_level': 'INFO',
    'source': 'env',
    'env_level': 'INFO',
    'override_level': null,
    'override_expires_at': null,
  };
  Map<String, dynamic>? lastSetLogLevelCall;
  Object? setLogLevelError;
  bool clearLogLevelCalled = false;

  @override
  Future<Map<String, dynamic>> getStats() async => {
        'totals': {
          'users': 3,
          'projects': 5,
          'activities': 40,
          'memories': 12,
          'storage_bytes': 2 * 1024 * 1024,
          'recent_signups_7d': 1,
        },
        'users': [
          {
            'id': 1, 'email': 'a@x.com', 'display_name': 'Alice',
            'auth_provider': 'local', 'created_at': 1700000000,
            'project_count': 2, 'activity_count': 10, 'memory_count': 3,
            'storage_bytes': 1024, 'encryption_tier': 'high', 'is_admin': false,
            'plan': 'tier_2', 'plan_name': 'Tier 2', 'is_comped': false,
            'subscription_status': 'active',
            'stripe_customer_url': 'https://dashboard.stripe.com/test/customers/cus_1',
          },
          {
            'id': 2, 'email': 'b@x.com', 'display_name': 'Bob',
            'auth_provider': 'local', 'created_at': 1700000000,
            'project_count': 1, 'activity_count': 1, 'memory_count': 0,
            'storage_bytes': 0, 'encryption_tier': 'none', 'is_admin': true,
            'plan': 'tier_1', 'plan_name': 'Tier 1', 'is_comped': true,
            'subscription_status': 'none', 'stripe_customer_url': null,
          },
        ],
      };

  @override
  Future<void> refreshStorage() async {}

  @override
  Future<List<Map<String, dynamic>>> searchUsers(String q, {int limit = 50}) async =>
      searchResult;

  @override
  Future<String> resetPassword(int userInfoId) async {
    resetCalledFor = userInfoId;
    return 'TEMP-PW-123';
  }

  @override
  Future<void> setAdmin(int userInfoId, bool isAdmin) async {
    if (setAdminError != null) throw setAdminError!;
    adminToggledFor = userInfoId;
    adminToggledTo = isAdmin;
  }

  @override
  Future<void> deleteUser(int userInfoId) async {
    if (deleteError != null) throw deleteError!;
    deleteCalledFor = userInfoId;
  }

  @override
  Future<int> broadcastEmail({
    required String subject,
    required String body,
    List<int> userIds = const [],
    bool sendToAll = false,
  }) async {
    if (broadcastEmailError != null) throw broadcastEmailError!;
    emailSentSubject = subject;
    emailSentBody = body;
    emailSentUserIds = userIds;
    emailSentToAll = sendToAll;
    return sendToAll ? 2 : userIds.length;
  }

  @override
  Future<Map<String, dynamic>> getLogLevel() async => logLevelState;

  @override
  Future<Map<String, dynamic>> setLogLevel(String level,
      {int? durationMinutes}) async {
    if (setLogLevelError != null) throw setLogLevelError!;
    lastSetLogLevelCall = {'level': level, 'durationMinutes': durationMinutes};
    logLevelState = {
      'effective_level': level,
      'source': 'override',
      'env_level': 'INFO',
      'override_level': level,
      'override_expires_at': durationMinutes != null
          ? DateTime.now().millisecondsSinceEpoch / 1000 + durationMinutes * 60
          : null,
    };
    return logLevelState;
  }

  @override
  Future<Map<String, dynamic>> clearLogLevelOverride() async {
    clearLogLevelCalled = true;
    logLevelState = {
      'effective_level': 'INFO',
      'source': 'env',
      'env_level': 'INFO',
      'override_level': null,
      'override_expires_at': null,
    };
    return logLevelState;
  }
}

Future<void> _pump(WidgetTester tester, Widget child) async {
  tester.view.physicalSize = const Size(1100, 1600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(MaterialApp(home: child));
  await tester.pumpAndSettle();
}

void main() {
  group('pure helpers', () {
    test('humanizeBytes', () {
      expect(humanizeBytes(512), '512 B');
      expect(humanizeBytes(1024), '1.0 KB');
      expect(humanizeBytes(2 * 1024 * 1024), '2.0 MB');
    });
    test('formatSignup', () {
      expect(formatSignup(0), '—');
      expect(formatSignup(1700000000), '2023-11-14');
    });
    test('tierColor maps tiers', () {
      expect(tierColor('high'), kSuccess);
      expect(tierColor('medium'), kWarning);
      expect(tierColor('none'), kColorOther);
    });
    test('logLevelColor maps levels', () {
      expect(logLevelColor('DEBUG'), kColorFlight);
      expect(logLevelColor('INFO'), kSuccess);
      expect(logLevelColor('WARNING'), kWarning);
      expect(logLevelColor('ERROR'), kAccent);
    });
  });

  // ── Logging (issue #208) ─────────────────────────────────────────────────────

  group('logging', () {
    testWidgets('shows the default level and no revert button', (tester) async {
      await _pump(tester, AdminScreen(service: _FakeAdminService()));
      expect(find.text('Logging'), findsOneWidget);
      expect(find.text('Default (INFO)'), findsOneWidget);
      expect(find.widgetWithText(OutlinedButton, 'Revert to default'),
          findsNothing);
    });

    testWidgets(
        'a short DEBUG override applies without a confirmation dialog',
        (tester) async {
      final svc = _FakeAdminService();
      await _pump(tester, AdminScreen(service: svc));

      // 'DEBUG' is unique before the chip updates (only the level segment).
      await tester.tap(find.text('DEBUG'));
      await tester.pumpAndSettle();
      // Default duration selection (15 min) is left as-is.
      await tester.tap(find.widgetWithText(FilledButton, 'Apply'));
      await tester.pumpAndSettle();

      expect(find.text('Apply indefinite DEBUG logging?'), findsNothing);
      expect(svc.lastSetLogLevelCall, {'level': 'DEBUG', 'durationMinutes': 15});
      expect(find.textContaining('Live override'), findsOneWidget);
      expect(find.widgetWithText(OutlinedButton, 'Revert to default'),
          findsOneWidget);
    });

    testWidgets(
        'an indefinite DEBUG override asks for confirmation; canceling '
        'leaves state unchanged', (tester) async {
      final svc = _FakeAdminService();
      await _pump(tester, AdminScreen(service: svc));

      await tester.tap(find.text('DEBUG'));
      await tester.pump();
      await tester.tap(find.text('Indefinite'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Apply'));
      await tester.pumpAndSettle();

      expect(find.text('Apply indefinite DEBUG logging?'), findsOneWidget);

      await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
      await tester.pumpAndSettle();

      expect(svc.lastSetLogLevelCall, isNull);
      expect(find.text('Default (INFO)'), findsOneWidget);
    });

    testWidgets('confirming the indefinite DEBUG dialog applies it',
        (tester) async {
      final svc = _FakeAdminService();
      await _pump(tester, AdminScreen(service: svc));

      await tester.tap(find.text('DEBUG'));
      await tester.pump();
      await tester.tap(find.text('Indefinite'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Apply'));
      await tester.pumpAndSettle();

      await tester.tap(find.widgetWithText(FilledButton, 'Apply anyway'));
      await tester.pumpAndSettle();

      expect(svc.lastSetLogLevelCall, {'level': 'DEBUG', 'durationMinutes': null});
      expect(find.text('Live override — until manually reverted'),
          findsOneWidget);
      expect(find.widgetWithText(OutlinedButton, 'Revert to default'),
          findsOneWidget);
    });

    testWidgets(
        'revert to default calls the service and drops the revert button',
        (tester) async {
      final svc = _FakeAdminService()
        ..logLevelState = {
          'effective_level': 'DEBUG',
          'source': 'override',
          'env_level': 'INFO',
          'override_level': 'DEBUG',
          'override_expires_at': null,
        };
      await _pump(tester, AdminScreen(service: svc));

      expect(find.widgetWithText(OutlinedButton, 'Revert to default'),
          findsOneWidget);
      await tester.tap(find.widgetWithText(OutlinedButton, 'Revert to default'));
      await tester.pumpAndSettle();

      expect(svc.clearLogLevelCalled, true);
      expect(find.text('Default (INFO)'), findsOneWidget);
      expect(find.widgetWithText(OutlinedButton, 'Revert to default'),
          findsNothing);
    });

    testWidgets('an apply failure surfaces a snackbar', (tester) async {
      final svc = _FakeAdminService()
        ..setLogLevelError =
            Exception('duration_minutes must be between 1 and 1440');
      await _pump(tester, AdminScreen(service: svc));

      await tester.tap(find.widgetWithText(FilledButton, 'Apply'));
      await tester.pumpAndSettle();

      expect(find.text('duration_minutes must be between 1 and 1440'),
          findsOneWidget);
    });
  });

  testWidgets('renders totals + a per-user row with its encryption tier',
      (tester) async {
    await _pump(tester, AdminScreen(service: _FakeAdminService()));
    expect(find.text('Users'), findsWidgets);
    expect(find.text('3'), findsWidgets); // user total
    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('high'), findsWidgets); // tier chip
    // Bob is admin, Alice isn't: exactly one shield icon in the table.
    expect(find.byIcon(Icons.shield), findsOneWidget);
  });

  testWidgets('tier chip carries an explanatory tooltip', (tester) async {
    await _pump(tester, AdminScreen(service: _FakeAdminService()));
    final tooltip = tester.widget<Tooltip>(
        find.ancestor(of: find.text('high'), matching: find.byType(Tooltip)).first);
    expect(tooltip.message, contains('Zero-knowledge'));
  });

  // ── Plan column (issue #194) ────────────────────────────────────────────────

  testWidgets('shows a plan chip per user, and a Stripe link only for the '
      'real subscriber', (tester) async {
    await _pump(tester, AdminScreen(service: _FakeAdminService()));
    expect(find.text('Tier 2'), findsOneWidget); // Alice: real subscription
    expect(find.text('Tier 1 · Granted'), findsOneWidget); // Bob: comped
    // Only Alice has a stripe_customer_url — exactly one link button.
    expect(find.byIcon(Icons.open_in_new), findsOneWidget);
  });

  testWidgets('comped plan chip tooltip calls out the admin override, not a '
      'real subscription', (tester) async {
    await _pump(tester, AdminScreen(service: _FakeAdminService()));
    final tooltip = tester.widget<Tooltip>(find.ancestor(
        of: find.text('Tier 1 · Granted'), matching: find.byType(Tooltip)).first);
    expect(tooltip.message, contains('Granted by an admin'));
  });

  testWidgets('tapping the Stripe link button opens the customer URL',
      (tester) async {
    String? openedUrl;
    await _pump(
      tester,
      AdminScreen(
        service: _FakeAdminService(),
        launcher: (url) async => openedUrl = url,
      ),
    );
    // The Users table scrolls horizontally and the link sits in the last
    // column, past the test viewport's right edge — scroll it into view
    // before tapping, or the tap misses (hits outside the render tree).
    await tester.ensureVisible(find.byIcon(Icons.open_in_new));
    await tester.tap(find.byIcon(Icons.open_in_new));
    await tester.pumpAndSettle();
    expect(openedUrl, 'https://dashboard.stripe.com/test/customers/cus_1');
  });

  testWidgets('search row also shows the plan chip and Stripe link',
      (tester) async {
    final svc = _FakeAdminService()
      ..searchResult = [
        {
          'id': 50, 'email': 'payer@x.com', 'display_name': 'Payer',
          'encryption_tier': 'none', 'is_admin': false,
          'plan': 'tier_3', 'plan_name': 'Tier 3', 'is_comped': false,
          'subscription_status': 'active',
          'stripe_customer_url': 'https://dashboard.stripe.com/test/customers/cus_2',
        },
      ];
    String? openedUrl;
    await _pump(
      tester,
      AdminScreen(service: svc, launcher: (url) async => openedUrl = url),
    );
    await tester.enterText(find.byKey(const Key('admin-search-field')), 'payer');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    expect(find.text('Tier 3'), findsOneWidget);
    // Two link buttons now: Alice's row in the Users table + this search row.
    expect(find.byIcon(Icons.open_in_new), findsNWidgets(2));

    await tester.tap(find.byIcon(Icons.open_in_new).last);
    await tester.pumpAndSettle();
    expect(openedUrl, 'https://dashboard.stripe.com/test/customers/cus_2');
  });

  testWidgets('a free user with no subscription shows the free plan chip '
      'and no Stripe link', (tester) async {
    final svc = _FakeAdminService()
      ..searchResult = [
        {
          'id': 51, 'email': 'free@x.com', 'display_name': 'Freeloader',
          'encryption_tier': 'none', 'is_admin': false,
          'plan': 'free', 'plan_name': 'Free', 'is_comped': false,
          'subscription_status': 'none', 'stripe_customer_url': null,
        },
      ];
    await _pump(tester, AdminScreen(service: svc));
    await tester.enterText(find.byKey(const Key('admin-search-field')), 'free');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    expect(find.text('Free'), findsOneWidget);
    // Only Alice's row (Users table) has a link — the free search result doesn't.
    expect(find.byIcon(Icons.open_in_new), findsOneWidget);
  });

  testWidgets('reset is enabled for None/Low, disabled for Medium/High',
      (tester) async {
    final svc = _FakeAdminService()
      ..searchResult = [
        {'id': 10, 'email': 'low@x.com', 'display_name': 'Low', 'encryption_tier': 'low'},
        {'id': 11, 'email': 'hi@x.com', 'display_name': 'Hi', 'encryption_tier': 'high'},
      ];
    await _pump(tester, AdminScreen(service: svc));

    await tester.enterText(find.byKey(const Key('admin-search-field')), 'x');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    final buttons = tester
        .widgetList<OutlinedButton>(find.widgetWithText(OutlinedButton, 'Reset password'))
        .toList();
    expect(buttons, hasLength(2));
    expect(buttons[0].onPressed, isNotNull); // low → enabled
    expect(buttons[1].onPressed, isNull); // high → disabled

    // The Logging card (issue #208) pushes this section below the fixed test
    // viewport — scroll the target into view before tapping, same reasoning
    // as the Stripe-link test above.
    await tester.ensureVisible(
        find.widgetWithText(OutlinedButton, 'Reset password').first);
    await tester.tap(find.widgetWithText(OutlinedButton, 'Reset password').first);
    await tester.pumpAndSettle();
    expect(svc.resetCalledFor, 10);
    expect(find.text('TEMP-PW-123'), findsOneWidget); // temp-password dialog
  });

  testWidgets('admin toggle grants and revokes, label flips after the call',
      (tester) async {
    final svc = _FakeAdminService()
      ..searchResult = [
        {'id': 20, 'email': 'plain@x.com', 'display_name': 'Plain', 'encryption_tier': 'none', 'is_admin': false},
      ];
    await _pump(tester, AdminScreen(service: svc));

    await tester.enterText(find.byKey(const Key('admin-search-field')), 'plain');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    expect(find.widgetWithText(OutlinedButton, 'Make admin'), findsOneWidget);
    await tester.tap(find.widgetWithText(OutlinedButton, 'Make admin'));
    await tester.pumpAndSettle();

    expect(svc.adminToggledFor, 20);
    expect(svc.adminToggledTo, true);
    expect(find.widgetWithText(OutlinedButton, 'Remove admin'), findsOneWidget);
  });

  testWidgets('admin toggle failure (e.g. self-revoke 409) surfaces a snackbar',
      (tester) async {
    final svc = _FakeAdminService()
      ..setAdminError = Exception('You cannot remove your own admin access.')
      ..searchResult = [
        {'id': 21, 'email': 'me@x.com', 'display_name': 'Me', 'encryption_tier': 'none', 'is_admin': true},
      ];
    await _pump(tester, AdminScreen(service: svc));

    await tester.enterText(find.byKey(const Key('admin-search-field')), 'me');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(OutlinedButton, 'Remove admin'));
    await tester.pumpAndSettle();

    expect(find.text('You cannot remove your own admin access.'), findsOneWidget);
    // Label stays as-is since the call failed.
    expect(find.widgetWithText(OutlinedButton, 'Remove admin'), findsOneWidget);
  });

  testWidgets('search row shows a shield icon for an admin user', (tester) async {
    final svc = _FakeAdminService()
      ..searchResult = [
        {'id': 22, 'email': 'admin2@x.com', 'display_name': 'Admin Two',
         'encryption_tier': 'none', 'is_admin': true},
      ];
    await _pump(tester, AdminScreen(service: svc));
    await tester.enterText(find.byKey(const Key('admin-search-field')), 'admin2');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();
    // One shield for Bob (admin, in the Users table from getStats) + one for
    // this search result.
    expect(find.byIcon(Icons.shield), findsNWidgets(2));
  });

  testWidgets('delete user prompts for confirmation, cancel makes no call',
      (tester) async {
    final svc = _FakeAdminService()
      ..searchResult = [
        {'id': 40, 'email': 'del@x.com', 'display_name': 'Deleteme',
         'encryption_tier': 'none', 'is_admin': false},
      ];
    await _pump(tester, AdminScreen(service: svc));
    await tester.enterText(find.byKey(const Key('admin-search-field')), 'del');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Delete user'));
    await tester.tap(find.widgetWithText(OutlinedButton, 'Delete user'));
    await tester.pumpAndSettle();
    expect(find.text('Delete user?'), findsOneWidget);

    await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
    await tester.pumpAndSettle();

    expect(svc.deleteCalledFor, isNull);
    expect(find.text('Deleteme'), findsOneWidget); // row still present
  });

  testWidgets('delete user confirmed removes the row and calls the service',
      (tester) async {
    final svc = _FakeAdminService()
      ..searchResult = [
        {'id': 41, 'email': 'del2@x.com', 'display_name': 'Deleteme2',
         'encryption_tier': 'none', 'is_admin': false},
      ];
    await _pump(tester, AdminScreen(service: svc));
    await tester.enterText(find.byKey(const Key('admin-search-field')), 'del2');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Delete user'));
    await tester.tap(find.widgetWithText(OutlinedButton, 'Delete user'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
    await tester.pumpAndSettle();

    expect(svc.deleteCalledFor, 41);
    expect(find.text('Deleteme2'), findsNothing); // row removed
  });

  testWidgets('delete user failure (e.g. self-delete 409) surfaces a snackbar',
      (tester) async {
    final svc = _FakeAdminService()
      ..deleteError = Exception('You cannot delete your own account here.')
      ..searchResult = [
        {'id': 42, 'email': 'me2@x.com', 'display_name': 'MeToo',
         'encryption_tier': 'none', 'is_admin': false},
      ];
    await _pump(tester, AdminScreen(service: svc));
    await tester.enterText(find.byKey(const Key('admin-search-field')), 'me2');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Delete user'));
    await tester.tap(find.widgetWithText(OutlinedButton, 'Delete user'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
    await tester.pumpAndSettle();

    expect(find.text('You cannot delete your own account here.'), findsOneWidget);
    expect(find.text('MeToo'), findsOneWidget); // row stays since the call failed
  });

  // The controls (chip + 3 buttons) must never squeeze the name/email column to
  // zero width: that made the email wrap one character per line and pushed the
  // buttons off the card. Asserting the widget exists is not enough — a 0 px
  // wide Text still "exists", so measure the rendered geometry.
  //
  // 560 is the layout's stacked/Row breakpoint; 700-900 is the range the Row
  // branch previously broke in. Stay >= 560 so the (pre-existing, unrelated)
  // _SectionCard header does not overflow.
  for (final width in [1400.0, 960.0, 900.0, 800.0, 700.0, 600.0, 560.0]) {
    testWidgets('search row keeps the email on one line at width $width',
        (tester) async {
      const email = 'a.long.user.name@example.com';
      final svc = _FakeAdminService()
        ..searchResult = [
          {
            'id': 30, 'email': email, 'display_name': 'Narrow Name',
            'encryption_tier': 'none', 'is_admin': false,
          },
        ];
      tester.view.physicalSize = Size(width, 900);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(MaterialApp(home: AdminScreen(service: svc)));
      await tester.pumpAndSettle();

      await tester.enterText(
          find.byKey(const Key('admin-search-field')), 'narrow');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(tester.takeException(), isNull); // no RenderFlex overflow
      expect(find.text('Narrow Name'), findsOneWidget);

      // A single bodySmall line; a per-character wrap of this email would be
      // ~28 lines tall and a few pixels wide.
      final size = tester.getSize(find.text(email));
      expect(size.height, lessThan(32), reason: 'email wrapped onto >1 line');
      expect(size.width, greaterThan(100), reason: 'email column squeezed');

      // The action buttons stay inside the card instead of being clipped away.
      for (final label in ['Reset password', 'Make admin', 'Delete user']) {
        final button = find.widgetWithText(OutlinedButton, label);
        expect(button, findsOneWidget);
        expect(tester.getTopRight(button).dx, lessThanOrEqualTo(width),
            reason: '"$label" is off-screen');
      }
    });
  }

  // ── Broadcast email ─────────────────────────────────────────────────────────

  testWidgets('selecting a user row then sending to selected calls the '
      'service with that id, subject and body', (tester) async {
    final svc = _FakeAdminService();
    await _pump(tester, AdminScreen(service: svc));

    // Checkbox order in the Users table: header (select-all), Alice, Bob.
    await tester.tap(find.byType(Checkbox).at(1));
    await tester.pumpAndSettle();

    // TextField order: subject, body (Send email section), then search.
    await tester.enterText(find.byType(TextField).at(0), 'Test subject');
    await tester.enterText(find.byType(TextField).at(1), 'Test body');
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Send to selected (1)'));
    await tester.pumpAndSettle();
    expect(find.text('Send email?'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Send'));
    await tester.pumpAndSettle();

    expect(svc.emailSentSubject, 'Test subject');
    expect(svc.emailSentBody, 'Test body');
    expect(svc.emailSentUserIds, [1]); // Alice's id
    expect(svc.emailSentToAll, false);
    expect(find.text('Queued for 1 recipient.'), findsOneWidget);
  });

  testWidgets('send to all works without any row selected', (tester) async {
    final svc = _FakeAdminService();
    await _pump(tester, AdminScreen(service: svc));

    await tester.enterText(find.byType(TextField).at(0), 'Subj');
    await tester.enterText(find.byType(TextField).at(1), 'Body');
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(OutlinedButton, 'Send to all 2'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Send'));
    await tester.pumpAndSettle();

    expect(svc.emailSentToAll, true);
    expect(find.text('Queued for 2 recipients.'), findsOneWidget);
  });

  testWidgets('the header checkbox selects every user row', (tester) async {
    final svc = _FakeAdminService();
    await _pump(tester, AdminScreen(service: svc));

    await tester.tap(find.byType(Checkbox).first); // select-all
    await tester.pumpAndSettle();

    expect(find.widgetWithText(FilledButton, 'Send to selected (2)'), findsOneWidget);
  });

  testWidgets('sending with an empty subject/body is blocked with a snackbar',
      (tester) async {
    final svc = _FakeAdminService();
    await _pump(tester, AdminScreen(service: svc));

    await tester.tap(find.byType(Checkbox).at(1));
    await tester.pumpAndSettle();
    // Subject/body left empty.
    await tester.tap(find.widgetWithText(FilledButton, 'Send to selected (1)'));
    await tester.pumpAndSettle();

    expect(find.text('Subject and body are required.'), findsOneWidget);
    expect(svc.emailSentSubject, isNull);
  });

  testWidgets('canceling the confirmation dialog sends nothing', (tester) async {
    final svc = _FakeAdminService();
    await _pump(tester, AdminScreen(service: svc));

    await tester.tap(find.byType(Checkbox).at(1));
    await tester.enterText(find.byType(TextField).at(0), 'Subj');
    await tester.enterText(find.byType(TextField).at(1), 'Body');
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Send to selected (1)'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
    await tester.pumpAndSettle();

    expect(svc.emailSentSubject, isNull);
  });
}
