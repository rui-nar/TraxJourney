/// The trip screen's encryption banner also says, on an unlocked device, how
/// many of the trip's activities stay unencrypted because another traveller
/// imported them (E2EE remnants decisions 6 and 14).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:traxjourney_client/src/crypto/encryption_locked_banner.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'encryption_service_test.dart' show FakeDeviceKeyStore, FakeEncryptionApi;

const _one = '1 activity was imported by another traveller and stays unencrypted.';

Future<void> _pump(WidgetTester tester, Widget banner) => tester.pumpWidget(
    MaterialApp(home: Scaffold(body: Column(children: [banner]))));

Future<EncryptionService> _unlocked(WidgetTester tester) async {
  final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
  await tester.runAsync(() => svc.enable(const RecoveryKeyChoice()));
  return svc;
}

void main() {
  testWidgets('shows the count of unencrypted activities when unlocked',
      (tester) async {
    final svc = await _unlocked(tester);
    final count = ValueNotifier<int>(1);
    await _pump(tester,
        EncryptionLockedBanner(service: svc, unencryptableActivityCount: count));

    expect(find.text(_one), findsOneWidget);

    count.value = 3;
    await tester.pump();
    expect(
        find.text('3 activities were imported by another traveller and stay '
            'unencrypted.'),
        findsOneWidget);
  });

  testWidgets('hidden for a count of 0', (tester) async {
    final svc = await _unlocked(tester);
    final count = ValueNotifier<int>(0);
    await _pump(tester,
        EncryptionLockedBanner(service: svc, unencryptableActivityCount: count));

    expect(find.byType(MaterialBanner), findsNothing);

    count.value = 1;
    await tester.pump();
    expect(find.text(_one), findsOneWidget);
    count.value = 0;
    await tester.pump();
    expect(find.byType(MaterialBanner), findsNothing);
  });

  testWidgets('hidden when locked, which shows the locked message instead',
      (tester) async {
    final svc = await _unlocked(tester);
    svc.lock();
    await _pump(
        tester,
        EncryptionLockedBanner(
            service: svc, unencryptableActivityCount: ValueNotifier<int>(1)));

    expect(find.text(_one), findsNothing);
    expect(find.text(kEncryptionLockedMessage), findsOneWidget);
  });

  testWidgets('hidden when encryption is off', (tester) async {
    final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
    await _pump(
        tester,
        EncryptionLockedBanner(
            service: svc, unencryptableActivityCount: ValueNotifier<int>(1)));

    expect(find.byType(MaterialBanner), findsNothing);
  });

  testWidgets("reads the open trip's count from the provided notifier",
      (tester) async {
    final svc = await _unlocked(tester);
    final notifier = ProjectNotifier(ProjectService());
    await tester.pumpWidget(ChangeNotifierProvider<ProjectNotifier>.value(
      value: notifier,
      child: MaterialApp(
          home: Scaffold(body: Column(children: [EncryptionLockedBanner(service: svc)]))),
    ));
    expect(find.byType(MaterialBanner), findsNothing);

    (notifier.unencryptableActivityCount as ValueNotifier<int>).value = 1;
    await tester.pump();
    expect(find.text(_one), findsOneWidget);
  });
}
