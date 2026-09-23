import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:share_plus/share_plus.dart';
import 'package:traxjourney_client/src/share/share_interfaces.dart';
import 'package:traxjourney_client/src/share/share_strategy.dart';
import 'package:traxjourney_client/src/share/social_share_controller.dart';

// ── Fakes ──────────────────────────────────────────────────────────────────

class _FakeCaps implements ShareCapabilities {
  @override
  final bool canShareFiles;
  const _FakeCaps(this.canShareFiles);
}

class _FakeAssets implements ShareAssetSource {
  int mapCalls = 0;
  bool? lastDayFocus;
  String? lastDate;
  List<String>? lastUuids;
  final Uint8List? mapBytes;

  _FakeAssets({this.mapBytes});

  @override
  Future<Uint8List?> renderMapImage(
      {required bool dayFocus, String? date}) async {
    mapCalls++;
    lastDayFocus = dayFocus;
    lastDate = date;
    return mapBytes;
  }

  @override
  Future<List<Uint8List>> fetchPhotos(int memoryId, List<String> uuids) async {
    lastUuids = uuids;
    return [for (final _ in uuids) Uint8List.fromList([1, 2, 3])];
  }
}

class _FakeLinks implements ShareLinkResolver {
  final String? link;
  String? lastPublicId;
  int calls = 0;
  _FakeLinks(this.link);

  @override
  Future<String?> resolveMemoryLink(String memoryPublicId) async {
    calls++;
    lastPublicId = memoryPublicId;
    return link;
  }
}

class _FakeTransport implements ShareTransport {
  List<XFile>? sharedFiles;
  String? sharedFilesText;
  String? textOnly;
  Uri? intentUri;
  String? clipboard;

  @override
  Future<void> shareFiles(List<XFile> files, {required String text}) async {
    sharedFiles = files;
    sharedFilesText = text;
  }

  @override
  Future<void> shareTextOnly(String text) async => textOnly = text;

  @override
  Future<void> shareUrlIntent(Uri uri) async => intentUri = uri;

  @override
  Future<void> copyToClipboard(String text) async => clipboard = text;
}

SocialShareController _make({
  required _FakeAssets assets,
  required _FakeLinks links,
  required _FakeTransport transport,
  required bool canShareFiles,
}) =>
    SocialShareController(
      assets: assets,
      links: links,
      transport: transport,
      caps: _FakeCaps(canShareFiles),
    );

void main() {
  group('SocialShareController.share', () {
    test('urlIntent target does NOT fetch assets and encodes text+link', () async {
      final assets = _FakeAssets(mapBytes: Uint8List.fromList([9]));
      final links = _FakeLinks('https://x/share/t?memory=pub1');
      final transport = _FakeTransport();
      final c = _make(
          assets: assets, links: links, transport: transport, canShareFiles: true);

      await c.share(
        target: ShareTarget.whatsapp,
        memoryId: 5,
        memoryPublicId: 'pub1',
        memoryDate: '2026-05-29',
        customText: 'Hi there',
        includeLink: true,
        includeMap: true,
        dayFocus: false,
        selectedPhotoUuids: const ['a'],
      );

      expect(assets.mapCalls, 0); // no asset rendering for URL intents
      expect(transport.sharedFiles, isNull);
      expect(transport.intentUri, isNotNull);
      final uri = transport.intentUri!.toString();
      expect(uri, startsWith('https://wa.me/?text='));
      expect(uri, contains(Uri.encodeComponent('Hi there')));
      expect(uri, contains(Uri.encodeComponent('https://x/share/t?memory=pub1')));
    });

    test('link resolver is called with the public_id, not the numeric id', () async {
      final links = _FakeLinks('https://x/l');
      final c = _make(
        assets: _FakeAssets(),
        links: links,
        transport: _FakeTransport(),
        canShareFiles: true,
      );

      await c.share(
        target: ShareTarget.facebook,
        memoryId: 42,
        memoryPublicId: 'public-uuid-xyz',
        memoryDate: null,
        customText: 'x',
        includeLink: true,
        includeMap: false,
        dayFocus: false,
        selectedPhotoUuids: const [],
      );

      expect(links.calls, 1);
      expect(links.lastPublicId, 'public-uuid-xyz');
    });

    test('sheetWithFiles renders map only when includeMap and attaches selected photos',
        () async {
      final assets = _FakeAssets(mapBytes: Uint8List.fromList([1, 2]));
      final transport = _FakeTransport();
      final c = _make(
        assets: assets,
        links: _FakeLinks('https://x/l'),
        transport: transport,
        canShareFiles: true,
      );

      await c.share(
        target: ShareTarget.system,
        memoryId: 7,
        memoryPublicId: 'pub',
        memoryDate: '2026-06-01',
        customText: 'Trip!',
        includeLink: true,
        includeMap: true,
        dayFocus: true,
        selectedPhotoUuids: const ['u1', 'u2'],
      );

      expect(assets.mapCalls, 1);
      expect(assets.lastDayFocus, isTrue);
      expect(assets.lastDate, '2026-06-01');
      expect(assets.lastUuids, ['u1', 'u2']);
      // 1 map + 2 photos = 3 files
      expect(transport.sharedFiles, hasLength(3));
      expect(transport.sharedFilesText, contains('Trip!'));
      expect(transport.sharedFilesText, contains('https://x/l'));
    });

    test('includeMap=false skips map render', () async {
      final assets = _FakeAssets(mapBytes: Uint8List.fromList([1]));
      final transport = _FakeTransport();
      final c = _make(
        assets: assets,
        links: _FakeLinks(null),
        transport: transport,
        canShareFiles: true,
      );

      await c.share(
        target: ShareTarget.system,
        memoryId: 1,
        memoryPublicId: 'p',
        memoryDate: null,
        customText: 'x',
        includeLink: false,
        includeMap: false,
        dayFocus: false,
        selectedPhotoUuids: const ['u1'],
      );

      expect(assets.mapCalls, 0);
      expect(transport.sharedFiles, hasLength(1)); // photo only
    });

    test('includeLink=false → resolver not called and no link in text', () async {
      final links = _FakeLinks('https://x/l');
      final transport = _FakeTransport();
      final c = _make(
        assets: _FakeAssets(),
        links: links,
        transport: transport,
        canShareFiles: true,
      );

      await c.share(
        target: ShareTarget.system,
        memoryId: 1,
        memoryPublicId: 'p',
        memoryDate: null,
        customText: 'Only body',
        includeLink: false,
        includeMap: false,
        dayFocus: false,
        selectedPhotoUuids: const ['u1'], // ensures a file so we hit shareFiles
      );

      expect(links.calls, 0);
      expect(transport.sharedFilesText, 'Only body');
      expect(transport.sharedFilesText, isNot(contains('http')));
    });

    test('copyLink copies the resolved link and fetches no assets', () async {
      final assets = _FakeAssets(mapBytes: Uint8List.fromList([1]));
      final links = _FakeLinks('https://x/share/t?memory=pub9');
      final transport = _FakeTransport();
      final c = _make(
          assets: assets, links: links, transport: transport, canShareFiles: true);

      await c.share(
        target: ShareTarget.copyLink,
        memoryId: 3,
        memoryPublicId: 'pub9',
        memoryDate: '2026-06-05',
        customText: 'ignored body',
        includeLink: true,
        includeMap: true,
        dayFocus: false,
        selectedPhotoUuids: const ['a'],
      );

      expect(transport.clipboard, 'https://x/share/t?memory=pub9');
      expect(assets.mapCalls, 0); // no rendering for clipboard
      expect(transport.sharedFiles, isNull);
      expect(transport.intentUri, isNull);
    });

    test('copyLink resolves the link even when includeLink is false', () async {
      final links = _FakeLinks('https://x/l9');
      final transport = _FakeTransport();
      final c = _make(
        assets: _FakeAssets(),
        links: links,
        transport: transport,
        canShareFiles: false,
      );

      await c.share(
        target: ShareTarget.copyLink,
        memoryId: 1,
        memoryPublicId: 'p',
        memoryDate: null,
        customText: 'body',
        includeLink: false,
        includeMap: false,
        dayFocus: false,
        selectedPhotoUuids: const [],
      );

      expect(links.calls, 1); // forced for copy-link despite includeLink=false
      expect(transport.clipboard, 'https://x/l9');
    });

    test('system target + no file capability → shareTextOnly', () async {
      final transport = _FakeTransport();
      final c = _make(
        assets: _FakeAssets(mapBytes: Uint8List.fromList([1])),
        links: _FakeLinks('https://x/l'),
        transport: transport,
        canShareFiles: false,
      );

      await c.share(
        target: ShareTarget.system,
        memoryId: 1,
        memoryPublicId: 'p',
        memoryDate: null,
        customText: 'Body',
        includeLink: true,
        includeMap: true,
        dayFocus: false,
        selectedPhotoUuids: const ['u1'],
      );

      expect(transport.sharedFiles, isNull);
      expect(transport.textOnly, contains('Body'));
      expect(transport.textOnly, contains('https://x/l'));
    });
  });
}
