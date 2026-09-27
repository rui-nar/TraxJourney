/// The memory and journal editors never show, clear or re-encrypt text this
/// device cannot decrypt (#466, review R1-1).
///
/// Such a field still holds its ciphertext envelope: the device is locked, or
/// the key is not this account's (a trip imported from another account). The
/// editor shows it as "Encrypted content unavailable", read-only, and a save
/// sends the envelope back exactly as it was, while every other field stays
/// editable. The server's update replaces every field, so leaving it out
/// would clear it.
///
/// Only that untouched envelope skips encryption: text the user types is
/// always encrypted when encryption is unlocked, even when it looks like an
/// envelope, such as "v1.2.3" (review R2-1), and on every later save too,
/// after the optimistic update or a reload has put that text back into the
/// items (review R3-1). The editors learn which field is undecrypted from the
/// record the notifier makes when it reveals the items, never from a value's
/// shape.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/encrypted_display.dart';
import 'package:traxjourney_client/src/crypto/encryption.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/journal_dialog.dart';
import 'package:traxjourney_client/src/projects/memory_dialog.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import '../crypto/encryption_service_test.dart' show FakeDeviceKeyStore, FakeEncryptionApi;

const _ref = ProjectRef(name: 'Trip');

/// Real envelopes made under another account's key: well formed, and no key
/// this device holds can decrypt them.
late String _nameEnv;
late String _descEnv;

/// The server's stored rows, what each PUT sent, and whether /meta fails.
late Map<String, dynamic> _memoryRow;
late Map<String, dynamic> _journalRow;
final List<Map<String, dynamic>> _puts = [];
var _metaFails = false;

void _fakeServer({required Map<String, dynamic> memory, required Map<String, dynamic> journal}) {
  _memoryRow = Map.of(memory);
  _journalRow = Map.of(journal);
  _puts.clear();
  _metaFails = false;
  api = ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        final path = req.url.path;
        if (req.method == 'PUT') {
          final body = jsonDecode(req.body) as Map<String, dynamic>;
          _puts.add(body);
          final row = path.startsWith('/api/memories/') ? _memoryRow : _journalRow;
          row
            ..remove('name')
            ..remove('description')
            ..addAll(body);
          return http.Response('', 204);
        }
        if (req.method == 'GET' && path.endsWith('/meta')) {
          if (_metaFails) return http.Response('{"detail":"offline"}', 503);
          return http.Response(
              jsonEncode({
                'name': 'Trip',
                'items': [
                  {'item_type': 'memory', 'memory': _memoryRow},
                  {'item_type': 'journal', 'journal': _journalRow},
                ],
              }),
              200);
        }
        return http.Response('{}', 200);
      }));
}

/// A notifier whose items came through the real load path, reveal included.
Future<ProjectNotifier> _loaded(WidgetTester tester) async {
  final notifier = ProjectNotifier(ProjectService())..ref = _ref;
  await tester.runAsync(() => notifier.reloadDetailsOnly(_ref));
  return notifier;
}

Map<String, dynamic> _item(ProjectNotifier n, String kind) =>
    Map.of(n.items.firstWhere((i) => i['item_type'] == kind)[kind] as Map<String, dynamic>);

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

Future<void> _openMemory(WidgetTester tester, ProjectNotifier n) =>
    _open(tester, MemoryDialog(notifier: n, editMemory: _item(n, 'memory')));

Future<void> _openJournal(WidgetTester tester, ProjectNotifier n) =>
    _open(tester, JournalDialog(notifier: n, editEntry: _item(n, 'journal')));

Future<void> _save(WidgetTester tester) async {
  await tester.tap(find.text('Save'));
  await tester.pumpAndSettle();
}

Future<void> _editAnotherFieldAndSave(WidgetTester tester) async {
  await tester.tap(find.text('End of day'));
  await tester.pumpAndSettle();
  await _save(tester);
}

TextField _field(WidgetTester tester, String text) =>
    tester.widget<TextField>(find.widgetWithText(TextField, text));

Future<String> _decrypted(WidgetTester tester, Object? sent) async =>
    (await tester.runAsync(() => encryption.decryptText(sent! as String)))!;

void main() {
  setUpAll(() async {
    final other = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
    await other.enable(const RecoveryKeyChoice());
    _nameEnv = await other.encryptText('Lac Blanc');
    _descEnv = await other.encryptText('Lunch');
  });

  Map<String, dynamic> memory() => {
        'id': 'm1', 'name': _nameEnv, 'description': _descEnv, 'date': '2026-01-01',
        'geo_mode': 'start_of_day', 'photos': <String>[],
      };
  Map<String, dynamic> journal() => {
        'id': 'j1', 'description': _descEnv, 'date': '2026-01-01',
        'geo_mode': 'start_of_day', 'photos': <String>[],
      };

  setUp(() {
    projectDataCache.resetForTest();
    _fakeServer(memory: memory(), journal: journal());
  });

  group('with encryption locked', () {
    testWidgets('the memory editor shows no ciphertext, and cannot edit it', (tester) async {
      await _openMemory(tester, await _loaded(tester));

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
      await _openMemory(tester, await _loaded(tester));

      await _editAnotherFieldAndSave(tester);

      final put = _puts.single;
      expect(put['geo_mode'], 'end_of_day');
      expect((put['name'], put['description']), (_nameEnv, _descEnv));
    });

    testWidgets('a readable memory is still edited as before', (tester) async {
      _fakeServer(
          memory: {...memory(), 'name': 'Lac Blanc', 'description': 'Lunch'},
          journal: journal());
      await _openMemory(tester, await _loaded(tester));

      expect(_field(tester, 'Lac Blanc').readOnly, isFalse);
      await tester.enterText(find.widgetWithText(TextField, 'Lac Blanc'), 'Lac Noir');
      await _save(tester);

      expect(_puts.single['name'], 'Lac Noir');
    });

    testWidgets('the journal editor shows no ciphertext, and a save keeps it',
        (tester) async {
      await _openJournal(tester, await _loaded(tester));

      expect(find.textContaining('v1.'), findsNothing);
      expect(find.text(kEncryptedUnavailable), findsOneWidget);
      await _editAnotherFieldAndSave(tester);

      final put = _puts.single;
      expect(put['geo_mode'], 'end_of_day');
      expect(put['description'], _descEnv);
    });
  });

  group('with encryption unlocked', () {
    // This device holds its own key; the envelopes above are another
    // account's, so it cannot decrypt them.
    setUp(() async {
      FlutterSecureStorage.setMockInitialValues({});
      await encryption.enable(const RecoveryKeyChoice());
      expect(encryption.isUnlocked, isTrue);
    });
    tearDown(() => encryption.lock());

    testWidgets('a memory it cannot decrypt is saved with its envelopes unchanged, twice',
        (tester) async {
      final notifier = await _loaded(tester);
      await _openMemory(tester, notifier);
      await _editAnotherFieldAndSave(tester);

      await _openMemory(tester, notifier);
      expect(find.text(kEncryptedUnavailable), findsNWidgets(2));
      await tester.tap(find.text('Start of day'));
      await tester.pumpAndSettle();
      await _save(tester);

      expect(_puts, hasLength(2));
      for (final put in _puts) {
        expect((put['name'], put['description']), (_nameEnv, _descEnv));
      }
    });

    testWidgets('a journal entry it cannot decrypt is saved with its envelope unchanged',
        (tester) async {
      await _openJournal(tester, await _loaded(tester));

      await _editAnotherFieldAndSave(tester);

      final put = _puts.single;
      expect(put['geo_mode'], 'end_of_day');
      expect(put['description'], _descEnv);
    });

    // A typed value shaped like an envelope is encrypted on the first save and
    // on the next one, whether the items then hold the optimistic update or a
    // reload's revealed text; and it stays editable in between.
    for (final reload in [false, true]) {
      final after = reload ? 'a reload' : 'the optimistic update';

      testWidgets('typed memory text shaped like an envelope is encrypted, '
          'again after $after', (tester) async {
        _fakeServer(
            memory: {...memory(), 'name': 'Lac Blanc', 'description': 'Lunch'},
            journal: journal());
        final notifier = await _loaded(tester);
        await _openMemory(tester, notifier);
        await tester.enterText(find.widgetWithText(TextField, 'Lac Blanc'), 'v1.2.3');
        await tester.enterText(
            find.widgetWithText(TextField, 'Lunch'), 'v1.Dinner with Dr. Smith');
        await _save(tester);
        if (reload) await tester.runAsync(() => notifier.reloadDetailsOnly(_ref));
        expect(_item(notifier, 'memory')['name'], 'v1.2.3');

        await _openMemory(tester, notifier);
        expect(_field(tester, 'v1.2.3').readOnly, isFalse);
        expect(_field(tester, 'v1.Dinner with Dr. Smith').readOnly, isFalse);
        await _editAnotherFieldAndSave(tester);

        expect(_puts, hasLength(2));
        for (final put in _puts) {
          expect(put['name'], isNot('v1.2.3'));
          expect(put['description'], isNot('v1.Dinner with Dr. Smith'));
          expect(await _decrypted(tester, put['name']), 'v1.2.3');
          expect(await _decrypted(tester, put['description']), 'v1.Dinner with Dr. Smith');
        }
      });

      testWidgets('a typed journal note shaped like an envelope is encrypted, '
          'again after $after', (tester) async {
        _fakeServer(memory: memory(), journal: {...journal(), 'description': 'Lunch'});
        final notifier = await _loaded(tester);
        // The journal's save reloads on its own; a failed reload leaves the
        // optimistic update in the items.
        _metaFails = !reload;
        await _openJournal(tester, notifier);
        await tester.enterText(find.widgetWithText(TextField, 'Lunch'), 'v1.2.3');
        await _save(tester);
        expect(_item(notifier, 'journal')['description'], 'v1.2.3');

        await _openJournal(tester, notifier);
        expect(_field(tester, 'v1.2.3').readOnly, isFalse);
        await _editAnotherFieldAndSave(tester);

        expect(_puts, hasLength(2));
        for (final put in _puts) {
          expect(put['description'], isNot('v1.2.3'));
          expect(await _decrypted(tester, put['description']), 'v1.2.3');
        }
      });
    }

    test('a kept value that is not a well-formed envelope is still encrypted', () async {
      // The backstop: even if an editor asked to keep typed text as stored,
      // only a well-formed envelope is resent as it is.
      final notifier = ProjectNotifier(ProjectService())
        ..ref = _ref
        ..items = [
          {'item_type': 'memory', 'memory': memory()},
          {'item_type': 'journal', 'journal': journal()},
        ];

      await notifier.updateMemory('m1',
          date: '2026-01-01', geoMode: 'start_of_day',
          name: 'v1.2.3', description: _descEnv,
          keepStoredName: true, keepStoredDescription: true);
      await notifier.updateJournal('j1',
          date: '2026-01-01', geoMode: 'start_of_day',
          description: 'v1.abcd.efgh', keepStoredDescription: true);

      final (memPut, journalPut) = (_puts[0], _puts[1]);
      expect(await encryption.decryptText(memPut['name'] as String), 'v1.2.3');
      expect(memPut['description'], _descEnv);
      expect(await encryption.decryptText(journalPut['description'] as String),
          'v1.abcd.efgh');
    });
  });
}
