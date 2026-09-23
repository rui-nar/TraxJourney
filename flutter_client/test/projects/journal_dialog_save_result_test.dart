// Regression tests for a mutation-propagation audit of the manual
// journal-add flow — mirrors memory_dialog_save_result_test.dart:
//
//  - Fix 3: JournalDialog._save() used to call createJournal and pop the
//    dialog unconditionally, even though createJournal swallowed its own
//    exceptions. A failed create looked successful to the user while a
//    phantom optimistic item lingered. createJournal now returns a bool the
//    dialog checks: the dialog stays open and shows the error inline (a
//    SnackBar via the root ScaffoldMessenger would render behind the modal
//    barrier and never be seen — see segment_dialog.dart's issue #20 fix,
//    which this follows).
//  - Fix 4: uploadJournalPhoto's return value (null on failure) was never
//    checked by _save(), so a corrupt image / dropped connection silently
//    vanished the photo while the entry still reported success. _save() now
//    counts failed uploads and surfaces them via a SnackBar once the entry
//    itself has saved.
library;

import 'dart:convert';
import 'dart:typed_data';

import 'package:cross_file/cross_file.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/journal_dialog.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

// A real (tiny, 1x1 transparent) PNG — Image.memory needs decodable bytes,
// not just any Uint8List, or the widget throws asynchronously mid-test.
final Uint8List _tinyPng = base64Decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=');

/// Fakes the file_picker plugin so tests can add a "pending photo" without
/// a real platform channel.
class _FakeFilePickerPlatform extends FilePickerPlatform {
  _FakeFilePickerPlatform(this.filesToReturn);
  final List<PlatformFile> Function() filesToReturn;

  @override
  Future<List<PlatformFile>> pickFiles({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    Function(FilePickerStatus)? onFileLoading,
    int compressionQuality = 0,
    AndroidOptions androidOptions = const AndroidOptions(),
    WindowsOptions windowsOptions = const WindowsOptions(),
    LinuxOptions linuxOptions = const LinuxOptions(),
    WebOptions webOptions = const WebOptions(),
  }) async =>
      filesToReturn();

  @override
  Future<PlatformFile?> pickFile({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    Function(FilePickerStatus)? onFileLoading,
    int compressionQuality = 0,
    AndroidOptions androidOptions = const AndroidOptions(),
    WindowsOptions windowsOptions = const WindowsOptions(),
    LinuxOptions linuxOptions = const LinuxOptions(),
    WebOptions webOptions = const WebOptions(),
  }) async {
    final files = filesToReturn();
    return files.isEmpty ? null : files.first;
  }
}

/// file_picker 12 made [PlatformFile] abstract and moved the bytes behind
/// readAsBytes(), so tests supply their own in-memory file rather than
/// constructing one from a byte list.
final class _FakePlatformFile extends PlatformFile {
  _FakePlatformFile({required this.name, required this.bytes});

  @override
  final String name;

  final Uint8List bytes;

  @override
  Uri get uri => Uri.dataFromBytes(bytes);

  @override
  XFile get xFile => XFile.fromData(bytes, name: name);

  @override
  Future<int> length() async => bytes.length;

  @override
  Future<Uint8List> readAsBytes() async => bytes;

  @override
  Stream<Uint8List> readAsByteStream() => Stream.value(bytes);
}

// Opens JournalDialog via a real showDialog(), like the app does — pushing
// an actual second route so Navigator.of(context).pop() inside _save()
// closes just the dialog, not the whole screen (and its ScaffoldMessenger,
// which the photo-upload-failure test below needs to survive the save).
Widget _harness(ProjectNotifier notifier) => MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) =>
                  JournalDialog(notifier: notifier, initialDate: '2026-01-01'),
            ),
            child: const Text('open'),
          ),
        ),
      ),
    );

void main() {
  setUp(() => projectDataCache.resetForTest());

  testWidgets(
      'a failed create keeps the dialog open and shows the error inline, not as a SnackBar',
      (tester) async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient(
            (req) async => http.Response('{"detail":"Server exploded"}', 500)));
    final notifier = ProjectNotifier(ProjectService())..ref = _ref;

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    // The dialog is still open — the failed create must not have popped it.
    expect(find.byType(AlertDialog), findsOneWidget);
    // The error is visible, inside the dialog...
    final errorFinder = find.text('Server exploded');
    expect(errorFinder, findsOneWidget);
    expect(
      find.descendant(of: find.byType(AlertDialog), matching: errorFinder),
      findsOneWidget,
    );
    // ...and not as a SnackBar (which would render behind the modal barrier).
    expect(find.widgetWithText(SnackBar, 'Server exploded'), findsNothing);
    // The optimistic placeholder must have been rolled back.
    expect(notifier.items, isEmpty);
  });

  testWidgets(
      'a successful create still saves when a pending photo upload fails, and the failure is reported',
      (tester) async {
    FilePickerPlatform.instance = _FakeFilePickerPlatform(() => [
          _FakePlatformFile(name: 'photo.png', bytes: _tinyPng),
        ]);
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/journal/') {
            return http.Response('{}', 200);
          }
          if (req.method == 'GET' &&
              req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                jsonEncode({
                  'name': 'Trip',
                  'items': [
                    {
                      'item_type': 'journal',
                      'journal': {
                        'id': 'journal-real-1',
                        'date': '2026-01-01',
                        'photos': <String>[],
                      },
                    },
                  ],
                }),
                200);
          }
          return http.Response('{}', 404);
        }));
    // No api.setToken(...) is called, so tokenForUpload is null and
    // uploadJournalPhoto returns null immediately — a deterministic stand-in
    // for "the photo upload failed" that needs no real network I/O.

    // Tall surface so the whole dialog (including "Add photos", below the
    // date/time/description/location sections) is on-screen and tappable
    // without needing to scroll the dialog's SingleChildScrollView first.
    tester.view.physicalSize = const Size(800, 1400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final notifier = ProjectNotifier(ProjectService())..ref = _ref;

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Add photos'));
    await tester.pumpAndSettle();
    expect(find.byType(Image), findsOneWidget,
        reason: 'the picked photo should show as a pending thumbnail');

    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    // The entry itself saved successfully — the dialog closed and the real
    // journal entry (not the placeholder) is in `items`.
    expect(find.byType(AlertDialog), findsNothing);
    expect(notifier.items, hasLength(1));
    expect(notifier.items.single['journal']['id'], 'journal-real-1');

    // The photo failure must still reach the user.
    expect(find.textContaining('photo failed to upload'), findsOneWidget);
  });
}
