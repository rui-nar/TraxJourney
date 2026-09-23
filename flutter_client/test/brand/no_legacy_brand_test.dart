// Guard for the TraxJourney rename (issue #151): the old product name must
// not creep back into the app's code, its web shell, or its platform
// projects. Every surviving occurrence is listed below with the reason it has
// to stay, matched by exact line content so nothing else can hide next to it.

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/brand.dart';

final _legacy = RegExp(r'view[ _-]?trip', caseSensitive: false);

/// Lines that must keep the old name, keyed by path then by trimmed line.
const _allowed = <String, Map<String, String>>{
  // Frozen protocol bytes (D1): the HKDF salt/info strings every stored key
  // wrap was derived with. Changing them makes existing wraps undecryptable;
  // test/crypto/e2ee_frozen_vectors_test.dart pins them.
  'lib/src/crypto/e2ee_crypto.dart': {
    "info: utf8.encode('viewtrip-e2ee/recovery-wrap/v1'),":
        'frozen HKDF info, recovery-key wrap',
    "final _deviceWrapSalt = utf8.encode('viewtrip-e2ee/device-wrap-salt/v1');":
        'frozen HKDF salt, device wrap',
    "info: utf8.encode('viewtrip-e2ee/device-wrap/v1'),":
        'frozen HKDF info, device wrap',
  },
  // D2: the pre-rename SharedPreferences key, read once so signed-in users
  // stay signed in; test/auth/auth_token_key_test.dart pins the migration.
  'lib/src/auth/auth_service.dart': {
    "static const _legacyTokenKey = 'viewtrip_jwt';":
        'legacy session token key, migrated on restore',
  },
};

/// Machine-generated, gitignored build inputs: they embed the checkout's
/// absolute path, which is not the app's to rename.
const _skipSegments = {'ephemeral', 'Pods', '.symlinks', 'build', '.dart_tool'};
const _skipFiles = {'Generated.xcconfig', 'flutter_export_environment.sh'};

/// Every text file the app ships or builds from. `flutter test` runs from the
/// package root.
List<File> _scannedFiles() {
  final files = <File>[
    File('pubspec.yaml'),
  ];
  for (final dir in ['lib', 'web', 'ios', 'macos', 'linux', 'windows']) {
    final d = Directory(dir);
    if (!d.existsSync()) continue;
    for (final e in d.listSync(recursive: true, followLinks: false)) {
      if (e is! File) continue;
      final parts = _rel(e).split('/');
      if (parts.any(_skipSegments.contains)) continue;
      if (_skipFiles.contains(parts.last)) continue;
      files.add(e);
    }
  }
  return files;
}

String _rel(File f) {
  var p = f.path.replaceAll(r'\', '/');
  if (p.startsWith('./')) p = p.substring(2);
  return p;
}

/// Legacy-name hits as path -> trimmed lines. Binary files are skipped.
Map<String, List<String>> _hits() {
  final hits = <String, List<String>>{};
  for (final f in _scannedFiles()) {
    final bytes = f.readAsBytesSync();
    if (bytes.contains(0)) continue;
    final text = utf8.decode(bytes, allowMalformed: true);
    for (final line in const LineSplitter().convert(text)) {
      if (_legacy.hasMatch(line)) {
        (hits[_rel(f)] ??= []).add(line.trim());
      }
    }
  }
  return hits;
}

void main() {
  final hits = _hits();

  test('the old name appears only where it has to', () {
    final unexpected = <String>[];
    hits.forEach((path, lines) {
      for (final line in lines) {
        if (_allowed[path]?.containsKey(line) ?? false) continue;
        unexpected.add('$path: $line');
      }
    });
    expect(unexpected, isEmpty,
        reason: 'rename these to $kAppName, or allowlist them with a reason');
  });

  test('every allowlisted line still exists', () {
    final stale = <String>[
      for (final entry in _allowed.entries)
        for (final line in entry.value.keys)
          if (!(hits[entry.key]?.contains(line) ?? false))
            '${entry.key}: $line',
    ];
    expect(stale, isEmpty, reason: 'remove these entries from the allowlist');
  });

  test('the scan covers the app', () {
    final scanned = _scannedFiles().map(_rel).toSet();
    expect(
        scanned,
        containsAll([
          'pubspec.yaml',
          'lib/main.dart',
          'web/index.html',
          'web/manifest.json',
          'ios/Runner/Info.plist',
          'macos/Runner/Configs/AppInfo.xcconfig',
          'linux/CMakeLists.txt',
          'windows/runner/Runner.rc',
        ]));
  });

  group('the name users see outside the app matches kAppName', () {
    test('web manifest', () {
      final manifest = jsonDecode(File('web/manifest.json').readAsStringSync())
          as Map<String, dynamic>;
      expect(manifest['name'], kAppName);
      expect(manifest['short_name'], kAppName);
    });

    test('web page title and home-screen title', () {
      final html = File('web/index.html').readAsStringSync();
      expect(html, contains('<title>$kAppName</title>'));
      expect(html,
          contains('<meta name="apple-mobile-web-app-title" content="$kAppName">'));
    });

    test('iOS display name', () {
      final plist = File('ios/Runner/Info.plist').readAsStringSync();
      expect(
          RegExp(r'<key>CFBundleDisplayName</key>\s*<string>([^<]*)</string>')
              .firstMatch(plist)
              ?.group(1),
          kAppName);
    });
  });
}
