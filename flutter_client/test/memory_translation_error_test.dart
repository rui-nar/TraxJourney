// Regression for issue #24: tapping a translation flag spun briefly and then
// did nothing — the failing request was swallowed by an empty `catch (_)`, so
// the user (and the logs) saw no error at all. The failure must now surface as
// a SnackBar so it's diagnosable.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/projects/memory_detail_modal.dart';
import 'package:traxjourney_client/src/projects/project_memory_crud_mixin.dart'
    show TranslationUnavailableException;
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Notifier whose translation call fails like the backend would, and whose
/// like/comment loaders are stubbed so the modal makes no real network calls.
class _FailingNotifier extends ProjectNotifier {
  _FailingNotifier() : super(ProjectService());

  @override
  Future<Map<String, dynamic>> fetchTranslation(String memoryId, String lang) async {
    throw ApiException(502, 'Translation service error');
  }

  @override
  Future<Map<String, dynamic>> fetchLikes(String memoryId) async =>
      {'count': 0, 'liked_by_me': false, 'likers': []};

  @override
  Future<List<Map<String, dynamic>>> fetchComments(String memoryId) async => [];
}

/// Notifier simulating an encrypted memory (issue #27) — mirrors the real
/// mixin's behaviour of throwing before any network call is attempted.
class _EncryptedMemoryNotifier extends ProjectNotifier {
  _EncryptedMemoryNotifier() : super(ProjectService());

  bool translationRequested = false;

  @override
  Future<Map<String, dynamic>> fetchTranslation(String memoryId, String lang) async {
    translationRequested = true;
    throw TranslationUnavailableException();
  }

  @override
  Future<Map<String, dynamic>> fetchLikes(String memoryId) async =>
      {'count': 0, 'liked_by_me': false, 'likers': []};

  @override
  Future<List<Map<String, dynamic>>> fetchComments(String memoryId) async => [];
}

void main() {
  testWidgets('a failed translation surfaces a SnackBar instead of failing silently',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 1600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final notifier = _FailingNotifier()..languages = ['fr'];
    final memory = {
      'id': 1,
      'date': '2025-06-01',
      'name': 'A place',
      'description': 'Some text',
      'photos': <String>[],
    };

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => Center(
            child: ElevatedButton(
              onPressed: () => showMemoryDetail(context, notifier, memory),
              child: const Text('open'),
            ),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    // No error before the user tries to translate.
    expect(find.byType(SnackBar), findsNothing);

    await tester.tap(find.text('🇫🇷'));
    await tester.pump();        // start the async translate
    await tester.pump();        // let the catch run + schedule the SnackBar
    await tester.pump();        // SnackBar enters

    expect(find.byType(SnackBar), findsOneWidget);
    expect(find.textContaining("Couldn't translate"), findsOneWidget);
    expect(find.textContaining('502'), findsOneWidget);
  });

  testWidgets(
      'an encrypted memory shows a distinct message, not "please try again" (#27)',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 1600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final notifier = _EncryptedMemoryNotifier()..languages = ['fr'];
    final memory = {
      'id': 1,
      'date': '2025-06-01',
      'name': 'A place',
      'description': 'Some text',
      'photos': <String>[],
    };

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => Center(
            child: ElevatedButton(
              onPressed: () => showMemoryDetail(context, notifier, memory),
              child: const Text('open'),
            ),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('🇫🇷'));
    await tester.pump();
    await tester.pump();
    await tester.pump();

    expect(notifier.translationRequested, isTrue);
    expect(find.byType(SnackBar), findsOneWidget);
    expect(find.textContaining("encrypted and can't be translated"), findsOneWidget);
    expect(find.textContaining('Please try again'), findsNothing);
  });

  // The "show original" reset button reads "Original" rather than a bare "✕":
  // there's no stored source language and a flag would misrepresent it, so the
  // word is clearer than an icon. See language-bar reset button.
  testWidgets('the original-language reset button is labelled "Original"',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 1600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final notifier = _FailingNotifier()..languages = ['fr'];
    final memory = {
      'id': 1,
      'date': '2025-06-01',
      'name': 'A place',
      'description': 'Some text',
      'photos': <String>[],
    };

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => Center(
            child: ElevatedButton(
              onPressed: () => showMemoryDetail(context, notifier, memory),
              child: const Text('open'),
            ),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('Original'), findsOneWidget);
    expect(find.text('✕'), findsNothing);
  });
}
