// Widget tests for the anonymous-viewer decrypt path (issue #28 Part B):
// given share_name_ciphertext/share_description_ciphertext plus the right
// fragment key, the modal must render the decrypted plaintext; given the
// wrong key or no key at all, it must render an explicit "unavailable"
// message rather than a blank title/description (mirrors the tone of the
// existing #27 "encrypted and can't be translated" copy).

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/share_crypto.dart';
import 'package:traxjourney_client/src/projects/memory_detail_modal.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Notifier whose like/comment loaders are stubbed so the modal makes no
/// real network calls (mirrors test/memory_translation_error_test.dart).
class _StubNotifier extends ProjectNotifier {
  _StubNotifier() : super(ProjectService());

  @override
  Future<Map<String, dynamic>> fetchLikes(String memoryId) async =>
      {'count': 0, 'liked_by_me': false, 'likers': []};

  @override
  Future<List<Map<String, dynamic>>> fetchComments(String memoryId) async => [];
}

Future<Map<String, dynamic>> _encryptedMemory() async {
  final key = await generateShareKey();
  final nameCt = await encryptTextWithKey('Beach at dusk', key);
  final descCt = await encryptTextWithKey('We watched the sun go down.', key);
  return {
    'memory': {
      'id': 1,
      'date': '2025-06-01',
      'name': null,
      'description': null,
      'name_encrypted': true,
      'description_encrypted': true,
      'share_name_ciphertext': nameCt,
      'share_description_ciphertext': descCt,
      'photos': <String>[],
    },
    'key': key,
  };
}

Future<void> _openModal(
  WidgetTester tester, {
  required Map<String, dynamic> memory,
  required dynamic shareContentKey,
}) async {
  tester.view.physicalSize = const Size(1200, 1600);
  tester.view.devicePixelRatio = 1.0;
  tester.view.resetPhysicalSize();
  tester.view.physicalSize = const Size(1200, 1600);
  addTearDown(tester.view.reset);

  final notifier = _StubNotifier()
    ..items = [
      {'item_type': 'memory', 'memory': memory},
    ];

  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => Center(
          child: ElevatedButton(
            onPressed: () => showMemoryDetail(
              context,
              notifier,
              memory,
              readOnly: true,
              shareToken: 'tok_full',
              shareContentKey: shareContentKey,
            ),
            child: const Text('open'),
          ),
        ),
      ),
    ),
  ));

  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('the right fragment key decrypts share content into view',
      (tester) async {
    final built = await _encryptedMemory();
    await _openModal(tester,
        memory: built['memory'] as Map<String, dynamic>,
        shareContentKey: built['key']);

    expect(find.text('Beach at dusk'), findsOneWidget);
    expect(find.text('We watched the sun go down.'), findsOneWidget);
    expect(find.text('This memory is private'), findsNothing);
  });

  testWidgets('the wrong fragment key renders the unavailable state, not garbage',
      (tester) async {
    final built = await _encryptedMemory();
    final wrongKey = await generateShareKey();
    await _openModal(tester,
        memory: built['memory'] as Map<String, dynamic>,
        shareContentKey: wrongKey);

    expect(find.text('Beach at dusk'), findsNothing);
    expect(find.text('This memory is private'), findsOneWidget);
    expect(find.text('Content is unavailable without the share key.'),
        findsOneWidget);
  });

  testWidgets('no fragment key at all renders the unavailable state',
      (tester) async {
    final built = await _encryptedMemory();
    await _openModal(tester,
        memory: built['memory'] as Map<String, dynamic>,
        shareContentKey: null);

    expect(find.text('Beach at dusk'), findsNothing);
    expect(find.text('This memory is private'), findsOneWidget);
    expect(find.text('Content is unavailable without the share key.'),
        findsOneWidget);
  });

  testWidgets('a plaintext memory (no share ciphertext) is unaffected',
      (tester) async {
    final memory = {
      'id': 2,
      'date': '2025-06-02',
      'name': 'A regular memory',
      'description': 'Nothing encrypted here.',
      'photos': <String>[],
    };
    await _openModal(tester, memory: memory, shareContentKey: null);

    expect(find.text('A regular memory'), findsOneWidget);
    expect(find.text('Nothing encrypted here.'), findsOneWidget);
    expect(find.text('This memory is private'), findsNothing);
  });
}
