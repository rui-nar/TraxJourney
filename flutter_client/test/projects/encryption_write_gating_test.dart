/// Memory, journal and Polarsteps writes follow the account's encryption
/// state and the trip's owner (#505, #506).
///
/// - While the account is encrypted and this device can't use the key
///   (locked, or awaiting approval), the editors disable Save with the
///   banner's message and send nothing, and the imports refuse to start.
/// - Memories are encrypted under the trip owner's key: this device encrypts
///   them only on a trip the user owns; a companion's memory text stays
///   plaintext. Journal entries are the author's own, encrypted anywhere.
/// - The server's encryption refusals (409) read as plain words.
///
/// These tests drive the app's real `encryption` singleton. It talks to the
/// server through the `api` client current when it is first used, so every
/// test here shares one client whose answers [_handler] decides.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart' show EncryptedField;
import 'package:traxjourney_client/src/crypto/encryption.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/journal_dialog.dart';
import 'package:traxjourney_client/src/projects/memory_dialog.dart';
import 'package:traxjourney_client/src/projects/polarsteps_import_notifier.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/sync_import_notifier.dart';

const _ownTrip = ProjectRef(name: 'Trip');
const _companionTrip = ProjectRef(name: 'Trip', ownerId: 7, role: 'editor');

/// Every content request the app sent (encryption endpoints excluded).
final List<http.Request> _sent = [];

/// What GET /api/encryption/status answers.
var _statusEnabled = false;

/// Answers to content writes; tests replace it to refuse with a 409.
http.Response Function(http.Request) _writeAnswer =
    (_) => http.Response('{"id": "m1"}', 200);

Future<http.Response> _handler(http.Request req) async {
  final path = req.url.path;
  if (path.startsWith('/api/encryption/')) {
    if (path == '/api/encryption/status') {
      return http.Response(
          jsonEncode({
            'enabled': _statusEnabled,
            'recovery_methods': ['recovery_key'],
            'device': {'registered': false, 'approved': false},
          }),
          200);
    }
    return http.Response('{}', 200);
  }
  _sent.add(req);
  if (req.method == 'GET' && path.endsWith('/meta')) {
    return http.Response(jsonEncode({'name': 'Trip', 'items': []}), 200);
  }
  return _writeAnswer(req);
}

Map<String, dynamic> _body(http.Request req) =>
    jsonDecode(req.body) as Map<String, dynamic>;

List<http.Request> _posts(String path) =>
    _sent.where((r) => r.method == 'POST' && r.url.path == path).toList();

/// Encrypted account, key unlocked on this device.
Future<void> _unlock() async {
  FlutterSecureStorage.setMockInitialValues({});
  await encryption.enable(const RecoveryKeyChoice());
}

/// Encrypted account, key dropped on this device.
Future<void> _lock() async {
  await _unlock();
  encryption.lock();
}

/// Encrypted account, this device not approved yet.
Future<void> _awaitApproval() async {
  encryption.lock();
  FlutterSecureStorage.setMockInitialValues({});
  _statusEnabled = true;
  await encryption.prepareForSession();
}

/// No encryption on the account.
Future<void> _disable() async {
  encryption.lock();
  _statusEnabled = false;
  await encryption.prepareForSession();
}

ProjectNotifier _notifier(ProjectRef ref) =>
    ProjectNotifier(ProjectService())..ref = ref;

Future<String> _decrypted(Object? value) =>
    encryption.decryptText(value! as String);

Future<void> _open(WidgetTester tester, Widget dialog) async {
  tester.view.physicalSize = const Size(1200, 2400);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showDialog<void>(context: context, builder: (_) => dialog),
          child: const Text('open'),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

ElevatedButton _saveButton(WidgetTester tester) => tester.widget<ElevatedButton>(
    find.ancestor(of: find.text('Save'), matching: find.byType(ElevatedButton)));

void main() {
  setUpAll(() {
    // Before anything touches `encryption`: it keeps the client it first sees.
    api = ApiClient(baseUrl: '', httpClient: MockClient(_handler));
    expect(encryption.state.value, EncryptionState.disabled);
  });

  setUp(() {
    projectDataCache.resetForTest();
    _sent.clear();
    _writeAnswer = (_) => http.Response('{"id": "m1"}', 200);
    api.clearToken();
  });

  tearDown(() async => _disable());

  group('editors', () {
    final blockedStates = {
      'locked': (_lock, kEncryptionLockedMessage),
      'awaiting approval': (_awaitApproval, kEncryptionAwaitingApprovalMessage),
    };

    for (final MapEntry(key: name, value: (enter, message)) in blockedStates.entries) {
      testWidgets('$name: the memory editor disables Save, says why, sends nothing',
          (tester) async {
        await tester.runAsync(enter);
        final notifier = _notifier(_ownTrip);
        await _open(tester,
            MemoryDialog(notifier: notifier, initialDate: '2026-01-01'));
        await tester.enterText(find.byType(TextField).first, 'Lac Blanc');
        await tester.pump();

        expect(find.text(message), findsOneWidget);
        expect(_saveButton(tester).onPressed, isNull);
        await tester.tap(find.text('Save'));
        await tester.pumpAndSettle();
        expect(_sent, isEmpty);

        // The notifier refuses on its own too, before any request.
        expect(
            await tester.runAsync(() =>
                notifier.createMemory(date: '2026-01-01', geoMode: 'start_of_day', name: 'x')),
            isFalse);
        expect(notifier.error, message);
        expect(notifier.itemsFacet.items, isEmpty);
        expect(_sent, isEmpty);
      });

      testWidgets('$name: the journal editor disables Save, says why, sends nothing',
          (tester) async {
        await tester.runAsync(enter);
        // On another traveller's trip too: the entry is the author's own.
        final notifier = _notifier(_companionTrip);
        await _open(tester,
            JournalDialog(notifier: notifier, initialDate: '2026-01-01'));

        expect(find.text(message), findsOneWidget);
        expect(_saveButton(tester).onPressed, isNull);
        await tester.tap(find.text('Save'));
        await tester.pumpAndSettle();
        expect(_sent, isEmpty);
      });
    }

    testWidgets("locked: a memory on another traveller's trip can still be saved, "
        'as plaintext', (tester) async {
      await tester.runAsync(_lock);
      final notifier = _notifier(_companionTrip);
      await _open(tester,
          MemoryDialog(notifier: notifier, initialDate: '2026-01-01'));

      expect(find.text(kEncryptionLockedMessage), findsNothing);
      expect(_saveButton(tester).onPressed, isNotNull);
    });

    testWidgets('unlocking while the editor is open enables Save', (tester) async {
      await tester.runAsync(_lock);
      await _open(tester,
          MemoryDialog(notifier: _notifier(_ownTrip), initialDate: '2026-01-01'));
      expect(_saveButton(tester).onPressed, isNull);

      await tester.runAsync(_unlock);
      await tester.pump();
      expect(find.text(kEncryptionLockedMessage), findsNothing);
      expect(_saveButton(tester).onPressed, isNotNull);
    });

    testWidgets('disabled: Save stays on and nothing is said', (tester) async {
      await _open(tester,
          MemoryDialog(notifier: _notifier(_ownTrip), initialDate: '2026-01-01'));
      expect(find.text(kEncryptionLockedMessage), findsNothing);
      expect(_saveButton(tester).onPressed, isNotNull);
    });
  });

  group('memory and journal writes, unlocked', () {
    setUp(_unlock);

    test('the owner sends memory text encrypted', () async {
      final ok = await _notifier(_ownTrip).createMemory(
          date: '2026-01-01', geoMode: 'start_of_day',
          name: 'Lac Blanc', description: 'Lunch');

      expect(ok, isTrue);
      final body = _body(_posts('/api/memories/').single);
      expect(EncryptedField.isWellFormed(body['name'] as String), isTrue);
      expect(await _decrypted(body['name']), 'Lac Blanc');
      expect(await _decrypted(body['description']), 'Lunch');
    });

    test("a companion sends memory text plaintext on another's trip", () async {
      final notifier = _notifier(_companionTrip);
      await notifier.createMemory(
          date: '2026-01-01', geoMode: 'start_of_day',
          name: 'Lac Blanc', description: 'Lunch');
      await notifier.updateMemory('m1',
          date: '2026-01-01', geoMode: 'start_of_day',
          name: 'Lac Noir', description: 'Dinner');

      final post = _body(_posts('/api/memories/').single);
      expect((post['name'], post['description']), ('Lac Blanc', 'Lunch'));
      final put = _body(_sent.singleWhere((r) => r.method == 'PUT'));
      expect((put['name'], put['description']), ('Lac Noir', 'Dinner'));
    });

    test("a companion's journal entry is encrypted with their own key", () async {
      final notifier = _notifier(_companionTrip);
      await notifier.createJournal(
          date: '2026-01-01', geoMode: 'start_of_day', description: 'Tired');

      final body = _body(_posts('/api/journal/').single);
      expect(await _decrypted(body['description']), 'Tired');
    });
  });

  group("the server's encryption refusals", () {
    for (final (code, words) in [
      ('encryption_locked', "can't encrypt"),
      ('encryption_not_shared', "isn't encrypted"),
    ]) {
      test('$code on a memory update is reported in plain words, '
          'and the update says it failed', () async {
        _writeAnswer = (_) => http.Response(
            jsonEncode({'detail': {'code': code}}), 409);
        final notifier = _notifier(_ownTrip);

        final ok = await notifier.updateMemory('m1',
            date: '2026-01-01', geoMode: 'start_of_day', name: 'Lac Blanc');

        expect(ok, isFalse);
        expect(notifier.error, contains(words));
      });

      test('$code on a journal update is reported in plain words', () async {
        _writeAnswer = (_) => http.Response(
            jsonEncode({'detail': {'code': code}}), 409);
        final notifier = _notifier(_ownTrip);

        final ok = await notifier.updateJournal('j1',
            date: '2026-01-01', geoMode: 'start_of_day', description: 'Tired');

        expect(ok, isFalse);
        expect(notifier.error, contains(words));
      });
    }

    testWidgets('a refused memory edit keeps the editor open with the message',
        (tester) async {
      _writeAnswer = (_) => http.Response(
          jsonEncode({'detail': {'code': 'encryption_not_shared'}}), 409);
      final notifier = _notifier(_ownTrip);
      final memory = {
        'id': 'm1', 'name': 'Lac Blanc', 'date': '2026-01-01',
        'geo_mode': 'start_of_day', 'photos': <String>[],
      };
      notifier.itemsFacetWriter.setItems([{'item_type': 'memory', 'memory': memory}]);
      await _open(tester, MemoryDialog(notifier: notifier, editMemory: memory));

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(find.byType(MemoryDialog), findsOneWidget);
      expect(find.textContaining("isn't encrypted"), findsOneWidget);
    });
  });

  group('Polarsteps import', () {
    final step = {
      'id': 11, 'name': 'Lac Blanc', 'description': 'Lunch',
      'date': '2026-01-01', 'photos': <Map<String, dynamic>>[],
    };

    PolarstepsImportNotifier importer() => PolarstepsImportNotifier(client: api)
      ..steps = [step]
      ..selectedStepIds.add(11);

    test('on an owned trip of an encrypted account, sends envelopes', () async {
      await _unlock();
      expect(await importer().importSelected(_ownTrip), 1);

      final body = _body(_posts('/api/memories/').single);
      expect(await _decrypted(body['name']), 'Lac Blanc');
      expect(await _decrypted(body['description']), 'Lunch');
      expect(body['polarsteps_step_id'], 11);
    });

    test('ownership comes from the session, not the ref\'s default role', () async {
      await _unlock();
      // A URL-built ref: owner id set, role left at its "owner" default.
      const urlRef = ProjectRef(name: 'Trip', ownerId: 7);

      api.setToken(_jwt(sub: 8));
      await importer().importSelected(urlRef);
      final asCompanion = _body(_posts('/api/memories/').last);
      expect(asCompanion['name'], 'Lac Blanc');

      api.setToken(_jwt(sub: 7));
      await importer().importSelected(urlRef);
      final asOwner = _body(_posts('/api/memories/').last);
      expect(await _decrypted(asOwner['name']), 'Lac Blanc');
    });

    test('refuses to start while locked', () async {
      await _lock();
      final n = importer();
      expect(await n.importSelected(_ownTrip), 0);
      expect(n.error, kEncryptionLockedMessage);
      expect(_sent, isEmpty);
    });

    test('refuses to start while awaiting approval', () async {
      await _awaitApproval();
      final n = importer();
      expect(await n.importSelected(_ownTrip), 0);
      expect(n.error, kEncryptionAwaitingApprovalMessage);
      expect(_sent, isEmpty);
    });

    test('a refused step says why in plain words', () async {
      _writeAnswer = (_) => http.Response(
          jsonEncode({'detail': {'code': 'encryption_locked'}}), 409);
      final n = importer();
      expect(await n.importSelected(_ownTrip), 0);
      expect(n.failedSteps.single.reason, contains("can't encrypt"));
    });
  });

  group('sync import', () {
    final step = {
      'id': 11, 'name': 'Lac Blanc', 'description': 'Lunch',
      'date': '2026-01-01', 'photos': <Map<String, dynamic>>[],
    };

    test('on an owned trip of an encrypted account, sends envelopes', () async {
      await _unlock();
      final n = SyncImportNotifier(
          stravaActivities: const [], psSteps: [step], apiClient: api);
      expect(await n.importSelected(_ownTrip), 1);

      final body = _body(_posts('/api/memories/').single);
      expect(await _decrypted(body['name']), 'Lac Blanc');
      expect(await _decrypted(body['description']), 'Lunch');
    });

    test('refuses to start while locked', () async {
      await _lock();
      final n = SyncImportNotifier(
          stravaActivities: [{'id': 5, 'name': 'Ride'}], psSteps: [step],
          apiClient: api);
      expect(await n.importSelected(_ownTrip), 0);
      expect(n.error, kEncryptionLockedMessage);
      expect(_sent, isEmpty);
    });

    test('Strava activities alone still import while locked', () async {
      await _lock();
      _writeAnswer = (_) => http.Response('{"added": 1}', 200);
      final n = SyncImportNotifier(
          stravaActivities: [{'id': 5, 'name': 'Ride'}], psSteps: const [],
          apiClient: api);
      expect(await n.importSelected(_ownTrip), 1);
    });
  });
}

/// An unsigned token carrying [sub]; the client only reads its claims.
String _jwt({required int sub}) {
  String part(Map<String, Object> m) =>
      base64Url.encode(utf8.encode(jsonEncode(m))).replaceAll('=', '');
  return '${part({'alg': 'none'})}.${part({'sub': '$sub'})}.sig';
}
