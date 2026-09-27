/// The memory and journal editors never show, clear or re-encrypt text this
/// device cannot decrypt (#466, review R1-1).
///
/// Such a field still holds its ciphertext envelope: the device is locked, or
/// the key is not this account's (a trip imported from another account). The
/// editor shows it as "Encrypted content unavailable", read-only, and a save
/// sends the envelope back exactly as it was, while every other field stays
/// editable. The server's update replaces every field, so leaving it out
/// would clear it.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/encrypted_display.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/journal_dialog.dart';
import 'package:traxjourney_client/src/projects/memory_dialog.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import '../crypto/encryption_service_test.dart' show FakeDeviceKeyStore, FakeEncryptionApi;

const _ref = ProjectRef(name: 'Trip');
const _nameEnv = 'v1.bmFtZUtleQ==.bmFtZUNpcGhlcg==';
const _descEnv = 'v1.ZGVzY0tleQ==.ZGVzY0NpcGhlcg==';

/// Records every PUT body the dialog's save sends.
final List<Map<String, dynamic>> _puts = [];

void _fakeServer() {
  _puts.clear();
  api = ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        if (req.method == 'PUT') {
          _puts.add(jsonDecode(req.body) as Map<String, dynamic>);
          return http.Response('', 204);
        }
        return http.Response('{}', 200);
      }));
}

Widget _harness(Widget dialog) => MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showDialog<void>(context: context, builder: (_) => dialog),
            child: const Text('open'),
          ),
        ),
      ),
    );

Future<void> _open(WidgetTester tester, Widget dialog) async {
  tester.view.physicalSize = const Size(1200, 2400);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(_harness(dialog));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

Future<void> _editAnotherFieldAndSave(WidgetTester tester) async {
  await tester.tap(find.text('End of day'));
  await tester.pumpAndSettle();
  await tester.tap(find.text('Save'));
  await tester.pumpAndSettle();
}

String _fieldText(WidgetTester tester, String label) => tester
    .widget<TextField>(find.widgetWithText(TextField, label))
    .controller!
    .text;

void main() {
  setUp(() {
    projectDataCache.resetForTest();
    _fakeServer();
  });

  final memory = {
    'id': 'm1', 'name': _nameEnv, 'description': _descEnv, 'date': '2026-01-01',
    'geo_mode': 'start_of_day', 'photos': <String>[],
  };
  final journal = {
    'id': 'j1', 'description': _descEnv, 'date': '2026-01-01',
    'geo_mode': 'start_of_day', 'photos': <String>[],
  };

  testWidgets('the memory editor shows no ciphertext, and cannot edit it', (tester) async {
    final notifier = ProjectNotifier(ProjectService())
      ..ref = _ref
      ..items = [{'item_type': 'memory', 'memory': Map.of(memory)}];

    await _open(tester, MemoryDialog(notifier: notifier, editMemory: Map.of(memory)));

    expect(find.textContaining('v1.'), findsNothing);
    expect(find.text(kEncryptedUnavailable), findsNWidgets(2));
    for (final field in tester.widgetList<TextField>(find.byType(TextField))) {
      if (field.controller?.text == kEncryptedUnavailable) {
        expect(field.readOnly, isTrue);
      }
    }
  });

  testWidgets('a memory saved after another edit sends its envelopes unchanged',
      (tester) async {
    final notifier = ProjectNotifier(ProjectService())
      ..ref = _ref
      ..items = [{'item_type': 'memory', 'memory': Map.of(memory)}];
    await _open(tester, MemoryDialog(notifier: notifier, editMemory: Map.of(memory)));

    await _editAnotherFieldAndSave(tester);

    final put = _puts.single;
    expect(put['geo_mode'], 'end_of_day');
    expect((put['name'], put['description']), (_nameEnv, _descEnv));
  });

  testWidgets('a readable memory is still edited as before', (tester) async {
    final plain = {...memory, 'name': 'Lac Blanc', 'description': 'Lunch'};
    final notifier = ProjectNotifier(ProjectService())
      ..ref = _ref
      ..items = [{'item_type': 'memory', 'memory': Map.of(plain)}];
    await _open(tester, MemoryDialog(notifier: notifier, editMemory: Map.of(plain)));

    expect(_fieldText(tester, 'Lac Blanc'), 'Lac Blanc');
    await tester.enterText(find.widgetWithText(TextField, 'Lac Blanc'), 'Lac Noir');
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(_puts.single['name'], 'Lac Noir');
  });

  testWidgets('the journal editor shows no ciphertext, and a save keeps it',
      (tester) async {
    final notifier = ProjectNotifier(ProjectService())
      ..ref = _ref
      ..items = [{'item_type': 'journal', 'journal': Map.of(journal)}];
    await _open(tester, JournalDialog(notifier: notifier, editEntry: Map.of(journal)));

    expect(find.textContaining('v1.'), findsNothing);
    expect(find.text(kEncryptedUnavailable), findsOneWidget);
    await _editAnotherFieldAndSave(tester);

    final put = _puts.single;
    expect(put['geo_mode'], 'end_of_day');
    expect(put['description'], _descEnv);
  });

  test('an unlocked device never encrypts an envelope again', () async {
    // The foreign-key case: this device is unlocked, but the envelope was
    // made under another account's key. Encrypting it again would bury the
    // original ciphertext inside a new one.
    final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi(),
        deviceLabel: 'Test');
    await svc.enable(const RecoveryKeyChoice());
    expect(svc.isUnlocked, isTrue);

    expect(await svc.protect(_nameEnv), _nameEnv);
    expect(await svc.protect('Lac Blanc'), isNot('Lac Blanc'));
  });
}
