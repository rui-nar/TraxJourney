// Only ProjectNotifier and its mixins write facet state (issue #294, the
// Conventions of docs/CLIENT_STATE_MAP_PLAN.md).
//
// A facet's own API is read-only; writing takes its ProjectFacetWriter, which
// the notifier holds. Dart cannot keep those writers from the rest of lib/:
// the mixins that must use them live in libraries of their own, so they are
// public. This test is the restriction instead: no file of lib/ other than
// the notifier, its mixins and the facets library may name a writer — the
// widgets, and also the notifier's two subclasses (ViewProjectNotifier,
// SharedProjectNotifier), which reach facet state through protected methods
// of ProjectNotifier.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Files allowed to write facets, relative to the package root.
bool _mayWrite(String path) =>
    path == 'lib/src/projects/project_notifier.dart' ||
    RegExp(r'^lib/src/projects/project_\w+_mixin\.dart$').hasMatch(path) ||
    path.startsWith('lib/src/projects/facets/');

/// A writer type (`GeoFacetWriter`, `ProjectFacetWriter`) or a notifier's
/// writer member (`geoFacetWriter`).
final _writer = RegExp(r'\w*FacetWriter\b');

/// Each `path:line: text` of [sources] (path → content) that names a writer
/// outside the files allowed to.
List<String> _violations(Map<String, String> sources) => [
      for (final e in sources.entries)
        if (!_mayWrite(e.key))
          for (final (i, line) in e.value.split('\n').indexed)
            // Code only: a comment may name a writer.
            if (_writer.hasMatch(line.split('//').first))
              '${e.key}:${i + 1}: ${line.trim()}',
    ];

Map<String, String> _lib() => {
      for (final f in Directory('lib').listSync(recursive: true))
        if (f is File && f.path.endsWith('.dart'))
          f.path.replaceAll(r'\', '/'): f.readAsStringSync(),
    };

void main() {
  test('no file outside the notifier, its mixins and the facets names a '
      'facet writer', () {
    expect(_violations(_lib()), isEmpty,
        reason: 'write facet state from ProjectNotifier or a mixin; a subclass '
            'calls a protected ProjectNotifier method that does');
  });

  test('the scan reads the files it guards and the ones it allows', () {
    final lib = _lib();
    // A scan that read nothing would pass vacuously.
    for (final path in [
      'lib/src/projects/view_screen.dart', // ViewProjectNotifier
      'lib/src/shared/shared_project_screen.dart', // SharedProjectNotifier
      'lib/src/projects/map_panel.dart',
    ]) {
      expect(lib, contains(path));
      expect(_mayWrite(path), isFalse, reason: path);
    }
    // The allowed files are where the writers are, so the pattern matches.
    expect(_writer.hasMatch(lib['lib/src/projects/project_notifier.dart']!),
        isTrue);
    expect(
        _writer.hasMatch(lib['lib/src/projects/facets/project_facet.dart']!),
        isTrue);
    expect(_mayWrite('lib/src/projects/project_segment_crud_mixin.dart'),
        isTrue);
    // Not every project_* file: these are screens.
    expect(_mayWrite('lib/src/projects/project_settings_screen.dart'), isFalse);
  });

  test('a subclass or a widget naming a writer fails the scan', () {
    final lib = _lib();
    for (final (path, line) in [
      // A subclass writing a facet directly (P2-R1-5).
      ('lib/src/projects/view_screen.dart', '    geoFacetWriter.markChanged();'),
      (
        'lib/src/shared/shared_project_screen.dart',
        '    final GeoFacetWriter w = geoFacetWriter;'
      ),
      // A widget reaching the writer through the notifier.
      (
        'lib/src/projects/map_panel.dart',
        '    context.read<ProjectNotifier>().styleFacetWriter.reset();'
      ),
    ]) {
      final mutated = {...lib, path: '${lib[path]}\n$line\n'};
      final found = _violations(mutated);
      expect(found, hasLength(1), reason: path);
      expect(found.single, allOf(startsWith('$path:'), endsWith(line.trim())));
    }
  });
}
