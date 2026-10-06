// ProjectNotifier.clear() has to drop everything a load and the session put
// in the app-wide notifier (issue #418, Decision 2 of
// docs/CLIENT_STATE_MAP_PLAN.md). It used to miss a dozen fields, and nothing
// noticed: a field added later is invisible to a behaviour test that lists
// the fields it knows about. Flutter has no reflection, so this reads the
// source instead.
//
// Every instance field declared by ProjectNotifier and its six mixins must
// either be written by clear() — directly, or in a method of these files that
// clear() calls, at any depth — or be on [_allowlist] below with the reason it
// survives a clear.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

const _dir = 'lib/src/projects';

/// The notifier and each mixin it is built from, with the file declaring it.
const _declarations = {
  'class ProjectNotifier': '$_dir/project_notifier.dart',
  'mixin ProjectFilterMixin': '$_dir/project_filter_mixin.dart',
  'mixin ProjectQuotaMixin': '$_dir/project_quota_mixin.dart',
  'mixin ProjectJournalCrudMixin': '$_dir/project_journal_crud_mixin.dart',
  'mixin ProjectMemoryCrudMixin': '$_dir/project_memory_crud_mixin.dart',
  'mixin ProjectPeopleCrudMixin': '$_dir/project_people_crud_mixin.dart',
  'mixin ProjectSegmentCrudMixin': '$_dir/project_segment_crud_mixin.dart',
};

/// Fields clear() deliberately leaves alone, each with why.
const _allowlist = {
  '_service': 'injected collaborator, not state',
  '_membersService': 'injected collaborator, not state',
  '_authUserId': 'the account check that calls clear(); resetting it would '
      'make the next account change look like the first',
  '_isDisposed': 'lifecycle of the notifier, not of a session',
  '_mapZoom': "the mounted map's camera zoom; AppScreen seeds it before a load",
  '_mapCameraActive': "the mounted map's gesture state, reported by the map",
  '_cameraIdleWaiter': 'holds no data; completes on the next camera idle or '
      'times out, and only loads clear() has invalidated wait on it',
  '_cameraIdleTimeouts': 'holds no data; each wait removes its own timeout, '
      'and dispose() cancels any left',
  '_refetchInFlight': "single-flight latch the running refetch releases in its "
      'own finally; clearing it would start a second refetch beside it',
  '_geoRefetchCount': 'session-wide perf diagnostic counter',
  'loadRetryBackoff': 'test seam, configured once',
  'zoomRefetchDebounce': 'test seam, configured once',
  'cameraIdleTimeout': 'test seam, configured once',
  'offlineSeedCoordinateCeiling': 'test seam, configured once',
  'immichStatusCacheTtl': 'test seam, configured once',
  'degradedRouteCheckInterval': 'test seam, configured once',
};

void main() {
  late Map<String, String> sources;

  setUpAll(() {
    sources = {
      for (final e in _declarations.entries)
        e.key: File(e.value).readAsStringSync(),
    };
  });

  test('every field is reset by clear() or allowlisted with a reason', () {
    final scan = _Scan(sources);
    expect(scan.unreset(), isEmpty,
        reason: 'reset these in ProjectNotifier.clear() (or a mixin reset it '
            'calls), or allowlist them in this test with the reason they '
            'survive an account change');
  });

  test('the scan sees the fields it is there to check', () {
    final scan = _Scan(sources);
    // A scanner that found nothing would pass the test above vacuously.
    expect(
        scan.fields,
        containsAll([
          'people', 'groups', 'selectedDays', '_heldStateKey', 'pendingSync',
          'shareToken', '_photoPollingTimer', '_degradedRouteCheckTimer',
          '_fullTrack', 'members', '_filters', 'quotaError',
          'polarstepsOverlaySteps',
          '_pendingSegmentPatches', '_segmentTombstones', '_removedSegments',
        ]));
    // Neither another class in the same file nor a mixin's abstract getters.
    expect(scan.fields, isNot(contains('_token'))); // _SupersessionTrack's
    expect(scan.fields, isNot(contains('index'))); // _RemovedSegment's
    expect(scan.fields, isNot(contains('projectRef'))); // abstract getter
  });

  test('every allowlist entry is a field that exists', () {
    final scan = _Scan(sources);
    expect(_allowlist.keys.where((f) => !scan.fields.contains(f)), isEmpty,
        reason: 'a stale entry would silently cover a new field of that name');
  });

  test('removing a reset from clear() fails the scan', () {
    for (final (field, line) in [
      ('people', 'people = [];'),
      ('_heldStateKey', '_heldStateKey = null;'),
      ('_removedSegments', 'resetSegmentState();'),
    ]) {
      final notifier = sources['class ProjectNotifier']!;
      final clearAt = notifier.indexOf('  void clear() {');
      final lineAt = notifier.indexOf(line, clearAt);
      expect(clearAt, isNonNegative);
      expect(lineAt, isNonNegative, reason: '$line is not in clear()');
      final mutated = notifier.replaceRange(lineAt, lineAt + line.length, '');
      final scan = _Scan({...sources, 'class ProjectNotifier': mutated});
      expect(scan.unreset(), contains(field));
    }
  });
}

/// The fields and methods of the declarations in [sources], read from source
/// with comments and string contents blanked out.
class _Scan {
  _Scan(Map<String, String> sources) {
    for (final e in sources.entries) {
      final code = _blank(e.value);
      final start = code.indexOf('${e.key} ');
      if (start < 0) throw StateError('${e.key} not found');
      final open = code.indexOf('{', start);
      _members(code.substring(open + 1, _matching(code, open)));
    }
  }

  final Set<String> fields = {};
  final Map<String, String> methods = {};

  /// Fields neither written by clear() nor allowlisted.
  Set<String> unreset() {
    final reached = <String>{'clear'};
    final queue = ['clear'];
    final text = StringBuffer();
    while (queue.isNotEmpty) {
      final body = methods[queue.removeLast()]!;
      text.writeln(body);
      for (final m in RegExp(r'\b([A-Za-z_]\w*)\s*\(').allMatches(body)) {
        final name = m.group(1)!;
        if (methods.containsKey(name) && reached.add(name)) queue.add(name);
      }
    }
    final reset = text.toString();
    return {
      for (final f in fields)
        if (!_allowlist.containsKey(f) && !_written(f, reset)) f,
    };
  }

  /// Whether [text] assigns [field], bumps it, or clears, resets, cancels or
  /// invalidates it in place.
  static bool _written(String field, String text) {
    final f = RegExp.escape(field);
    return RegExp('(?<![\\w.])$f\\s*(=(?![=>])|\\?\\?=|\\+\\+|--)'
            '|(\\+\\+|--)$f\\b'
            '|(?<![\\w.])$f\\s*\\??\\.(clear|reset|cancel|invalidate)\\('
            '|(?<![\\w.])$f\\.value\\s*=(?!=)')
        .hasMatch(text);
  }

  /// Splits a class body into members and records each field and method.
  void _members(String body) {
    var i = 0;
    while (i < body.length) {
      // One member: up to a `;` at depth 0, or a block body.
      var depth = 0;
      var sawAssign = false;
      final start = i;
      String? blockBody;
      for (; i < body.length; i++) {
        final c = body[i];
        // Not `<`: it is also less-than, and generics hold no `;`, `{` or `=`.
        if (c == '(' || c == '[') depth++;
        if (c == ')' || c == ']') depth--;
        if (depth == 0 && c == '=') sawAssign = true;
        if (c == '{') {
          final end = _matching(body, i);
          if (depth == 0 && !sawAssign) {
            blockBody = body.substring(i + 1, end);
            i = end + 1;
            break;
          }
          i = end;
          continue;
        }
        if (depth == 0 && c == ';') {
          i++;
          break;
        }
      }
      final text = body.substring(start, i).trim();
      if (text.isEmpty) continue;
      if (blockBody != null) {
        final name = _methodName(text.substring(0, text.indexOf('{')));
        if (name != null) methods[name] = blockBody;
      } else {
        final field = _fieldName(text);
        if (field != null) fields.add(field);
      }
    }
  }

  /// The declared name when [member] (ending in `;`) is an instance field.
  static String? _fieldName(String member) {
    var head = member.substring(0, member.length - 1);
    final arrow = head.indexOf('=>');
    final assign = RegExp(r'=(?![=>])').firstMatch(head)?.start ?? -1;
    if (arrow >= 0 && (assign < 0 || arrow < assign)) return null; // `=> ...`
    if (assign >= 0) head = head.substring(0, assign);
    head = head.replaceAll(RegExp(r'@\w+(\([^)]*\))?'), ' ').trim();
    if (RegExp(r'\b(static|get|set|operator|factory)\b').hasMatch(head)) {
      return null;
    }
    if (head.endsWith(')') || head.contains(':')) return null; // method, ctor
    return RegExp(r'(_?[A-Za-z]\w*)$').firstMatch(head)?.group(1);
  }

  /// The name of the method or constructor whose header is [head].
  static String? _methodName(String head) {
    // The parameter list is the last bracket group at depth 0.
    var depth = 0;
    var open = -1;
    for (var i = 0; i < head.length; i++) {
      if (head[i] == '(') {
        if (depth == 0) open = i;
        depth++;
      } else if (head[i] == ')') {
        depth--;
      }
    }
    if (open < 0) {
      // A getter with a block body.
      return RegExp(r'\bget\s+(\w+)').firstMatch(head)?.group(1);
    }
    final before = head.substring(0, open).replaceAll(RegExp(r'<[^<>]*>\s*$'), '');
    return RegExp(r'(\w+)\s*$').firstMatch(before)?.group(1);
  }

  /// Index of the `}` closing the `{` at [open] in blanked [code].
  static int _matching(String code, int open) {
    var depth = 0;
    for (var i = open; i < code.length; i++) {
      if (code[i] == '{') depth++;
      if (code[i] == '}' && --depth == 0) return i;
    }
    throw StateError('unbalanced braces');
  }

  /// [src] with comments and the text of string literals replaced by spaces,
  /// interpolated expressions kept, so brackets and names can be counted.
  static String _blank(String src) {
    final out = StringBuffer();
    var i = 0;

    void code({required bool untilBrace}) {
      var depth = 0;
      while (i < src.length) {
        final c = src[i];
        if (src.startsWith('//', i)) {
          while (i < src.length && src[i] != '\n') {
            out.write(' ');
            i++;
          }
        } else if (src.startsWith('/*', i)) {
          final end = src.indexOf('*/', i) + 2;
          out.write(src.substring(i, end).replaceAll(RegExp(r'[^\n]'), ' '));
          i = end;
        } else if (c == "'" || c == '"') {
          final raw = i > 0 && src[i - 1] == 'r';
          final triple = src.startsWith(c * 3, i);
          final quote = triple ? c * 3 : c;
          out.write(quote);
          i += quote.length;
          while (i < src.length && !src.startsWith(quote, i)) {
            if (!raw && src[i] == r'\') {
              out.write('  ');
              i += 2;
            } else if (!raw && src.startsWith(r'${', i)) {
              out.write(r'${');
              i += 2;
              code(untilBrace: true);
            } else {
              out.write(src[i] == '\n' ? '\n' : ' ');
              i++;
            }
          }
          out.write(quote);
          i += quote.length;
        } else {
          if (c == '{') depth++;
          if (c == '}') {
            if (untilBrace && depth == 0) {
              out.write('}');
              i++;
              return;
            }
            depth--;
          }
          out.write(c);
          i++;
        }
      }
    }

    code(untilBrace: false);
    return out.toString();
  }
}
