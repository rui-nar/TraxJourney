import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:share_plus/share_plus.dart';

import 'package:traxjourney_client/src/projects/social_share_dialog.dart';
import 'package:traxjourney_client/src/share/share_interfaces.dart';
import 'package:traxjourney_client/src/share/social_share_controller.dart';

// ── Recording fakes (mirror social_share_controller_test) ───────────────────

class _FakeCaps implements ShareCapabilities {
  @override
  final bool canShareFiles;
  const _FakeCaps(this.canShareFiles);
}

class _FakeAssets implements ShareAssetSource {
  @override
  Future<Uint8List?> renderMapImage(
          {required bool dayFocus, String? date}) async =>
      null;
  @override
  Future<List<Uint8List>> fetchPhotos(int memoryId, List<String> uuids) async =>
      [for (final _ in uuids) Uint8List.fromList([1])];
}

class _FakeLinks implements ShareLinkResolver {
  final String link;
  _FakeLinks(this.link);
  @override
  Future<String?> resolveMemoryLink(String memoryPublicId) async => link;
}

class _FakeTransport implements ShareTransport {
  List<XFile>? sharedFiles;
  String? textOnly;
  Uri? intentUri;
  String? clipboard;

  @override
  Future<void> shareFiles(List<XFile> files, {required String text}) async =>
      sharedFiles = files;
  @override
  Future<void> shareTextOnly(String text) async => textOnly = text;
  @override
  Future<void> shareUrlIntent(Uri uri) async => intentUri = uri;
  @override
  Future<void> copyToClipboard(String text) async => clipboard = text;
}

final _memories = <Map<String, dynamic>>[
  {
    'id': 1,
    'public_id': 'p1',
    'date': '2026-06-04',
    'name': 'Day one',
    'description': 'Hello',
    'photos': ['u1', 'u2'],
  },
  {
    'id': 2,
    'public_id': 'p2',
    'date': '2026-06-05',
    'name': 'Vajukoski',
    'description': 'World',
    'photos': <String>[],
  },
];

/// Pushes the modal as a route so the success-path `Navigator.pop()` is safe.
Future<_FakeTransport> _pump(WidgetTester tester) async {
  final transport = _FakeTransport();
  final controller = SocialShareController(
    assets: _FakeAssets(),
    links: _FakeLinks('https://x/share/t?memory=p1'),
    transport: transport,
    caps: const _FakeCaps(true),
  );

  final modal = SocialShareModal(
    allMemories: _memories,
    initialMemoryPublicId: 'p1',
    controller: controller,
    caps: const _FakeCaps(true),
    thumbUrl: (memId, uuid) => 'https://example/$memId/$uuid',
    authHeaders: const {},
  );

  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (ctx) => ElevatedButton(
          onPressed: () => Navigator.of(ctx).push(
            MaterialPageRoute<void>(builder: (_) => Scaffold(body: modal)),
          ),
          child: const Text('open'),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  return transport;
}

void main() {
  setUpAll(() => GoogleFonts.config.allowRuntimeFetching = false);

  testWidgets('renders header and the three include toggles', (tester) async {
    await _pump(tester);
    expect(find.text('Share memory'), findsOneWidget);
    expect(find.text('Pick what to include, then where to send it'),
        findsOneWidget);
    expect(find.text('Trip map'), findsOneWidget);
    expect(find.text('Zoom to this day'), findsOneWidget);
    expect(find.text('Link to memory'), findsOneWidget);
  });

  testWidgets('Zoom toggle is enabled with map on, disabled when map is off',
      (tester) async {
    await _pump(tester);
    // Order of switches: Trip map (0), Zoom (1), Link (2).
    Switch zoom() => tester.widget<Switch>(find.byType(Switch).at(1));
    expect(zoom().onChanged, isNotNull); // map on by default

    await tester.tap(find.byType(Switch).at(0)); // turn Trip map off
    await tester.pump();
    expect(zoom().onChanged, isNull);
  });

  testWidgets('tapping a photo updates the "N of M" count', (tester) async {
    await _pump(tester);
    expect(find.text('PHOTOS · 2 OF 2'), findsOneWidget); // both selected

    final photo = find.byKey(const ValueKey('share_photo_u1'));
    await tester.ensureVisible(photo); // scroll body clear of the pinned footer
    await tester.pumpAndSettle();
    await tester.tap(photo); // deselect
    await tester.pump();
    expect(find.text('PHOTOS · 1 OF 2'), findsOneWidget);
  });

  testWidgets('the message counter tracks the text length', (tester) async {
    await _pump(tester);
    expect(find.text('5 / 2000'), findsOneWidget); // "Hello"

    await tester.enterText(find.byType(TextField), 'Hello world');
    await tester.pump();
    expect(find.text('11 / 2000'), findsOneWidget);
  });

  testWidgets('WhatsApp fires a url intent', (tester) async {
    final t = await _pump(tester);
    await tester.tap(find.text('WhatsApp'));
    await tester.pumpAndSettle();
    expect(t.intentUri.toString(), startsWith('https://wa.me/'));
  });

  testWidgets('Facebook fires a url intent', (tester) async {
    final t = await _pump(tester);
    await tester.tap(find.text('Facebook'));
    await tester.pumpAndSettle();
    expect(t.intentUri.toString(), contains('facebook.com'));
  });

  testWidgets('Copy link copies the resolved link', (tester) async {
    final t = await _pump(tester);
    await tester.tap(find.text('Copy link'));
    await tester.pumpAndSettle();
    expect(t.clipboard, 'https://x/share/t?memory=p1');
  });

  testWidgets('Share… hands off to the system sheet with files', (tester) async {
    final t = await _pump(tester);
    await tester.tap(find.text('Share…'));
    await tester.pumpAndSettle();
    expect(t.sharedFiles, isNotNull); // 2 selected photos attached
  });
}
