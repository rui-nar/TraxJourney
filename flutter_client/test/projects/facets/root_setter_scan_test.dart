// The root's "changed" flag is complete by construction (issue #294, Decision
// 17 and P2-R1-3 of docs/CLIENT_STATE_MAP_PLAN.md).
//
// ProjectNotifier notifies its own listeners only when root state — anything
// not in a facet — changed since its last notify. A root write that does not
// mark the root changed would leave a banner or a spinner showing the old
// state until something else changes. So every root field is private behind
// a setter that marks the root changed, and Dart's privacy keeps the mixins
// and the two subclasses (other libraries) off the backing fields. This test
// reads the source for what the compiler cannot see inside
// project_notifier.dart itself, and fails on
//  - a public mutable field in the notifier, its mixins or its subclasses
//    (it would be root state no setter marks), other than the test seams;
//  - a setter of the notifier that does not write its backing field and mark
//    the root changed;
//  - an assignment to a backing field anywhere but its own setter;
//  - a root collection changed in place (`members.add(…)`), which changes
//    root state without an assignment.
// It is modelled on project_notifier_clear_scan_test.dart.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

const _dir = 'lib/src/projects';

/// The notifier, its mixins and its two subclasses, with their files.
const _declarations = {
  'class ProjectNotifier': '$_dir/project_notifier.dart',
  'mixin ProjectFilterMixin': '$_dir/project_filter_mixin.dart',
  'mixin ProjectQuotaMixin': '$_dir/project_quota_mixin.dart',
  'mixin ProjectJournalCrudMixin': '$_dir/project_journal_crud_mixin.dart',
  'mixin ProjectMemoryCrudMixin': '$_dir/project_memory_crud_mixin.dart',
  'mixin ProjectPeopleCrudMixin': '$_dir/project_people_crud_mixin.dart',
  'mixin ProjectSegmentCrudMixin': '$_dir/project_segment_crud_mixin.dart',
  'class ViewProjectNotifier': '$_dir/view_screen.dart',
  'class SharedProjectNotifier': 'lib/src/shared/shared_project_screen.dart',
};

/// Public mutable fields that are not root state: configuration a test sets
/// once, which no listener reads.
const _testSeams = {
  'loadRetryBackoff',
  'zoomRefetchDebounce',
  'cameraIdleTimeout',
  'offlineSeedCoordinateCeiling',
  'immichStatusCacheTtl',
  'degradedRouteCheckInterval',
};

const _mark = '_markRootChanged();';

void main() {
  late Map<String, String> sources;

  setUpAll(() {
    sources = {
      for (final e in _declarations.entries)
        e.key: File(e.value).readAsStringSync().replaceAll('\r\n', '\n'),
    };
  });

  test('the notifier, its mixins and subclasses hold no unmarked root state',
      () {
    expect(_Scan(sources).problems(), isEmpty);
  });

  test('the scan sees the setters it is there to check', () {
    final scan = _Scan(sources);
    // A scan that found nothing would pass the test above vacuously.
    expect(
        scan.backingFields,
        containsAll([
          '_ref', '_isLoading', '_error', '_loadErrorStatus',
          '_offlineFromCache', '_isMetaLoaded', '_isElevationLoaded',
          '_isSyncMetaLoaded', '_shareToken', '_shareTokenNoMemories',
          '_autoSyncEnabled', '_linkedPsTripId', '_lastStravaSyncAt',
          '_lastPsSyncAt', '_pendingSync', '_degradedRouteUpgradeAvailable',
          '_members', '_memberInviteToken', '_memberInviteRole',
          '_pendingInvites', '_quotaError', '_polarstepsOverlaySteps',
          '_polarstepsOverlayLabel',
        ]));
    // Every test seam still exists: a stale entry would cover a new field.
    expect(_testSeams.where((f) => !scan.publicFields.contains(f)), isEmpty);
  });

  group('the scan fails on', () {
    String inject(String declaration, String anchor, String code) {
      final src = sources[declaration]!;
      final at = src.indexOf(anchor);
      expect(at, isNonNegative, reason: '$anchor not found');
      return src.replaceRange(at + anchor.length, at + anchor.length, code);
    }

    test('a write to a backing field outside its setter', () {
      final mutated =
          inject('class ProjectNotifier', '  void clear() {', ' _isLoading = true;');
      expect(_Scan({...sources, 'class ProjectNotifier': mutated}).problems(),
          ['_isLoading written outside its setter']);
    });

    test('a compound write, and one through `this.`', () {
      final mutated = inject('class ProjectNotifier', '  void clear() {',
          ' this._linkedPsTripId = 1; _pendingSync ??= null;');
      expect(
          _Scan({...sources, 'class ProjectNotifier': mutated}).problems(),
          unorderedEquals([
            '_linkedPsTripId written outside its setter',
            '_pendingSync written outside its setter',
          ]));
    });

    test('a setter that does not mark the root changed', () {
      final src = sources['class ProjectNotifier']!;
      final setter = src.indexOf('set shareToken(');
      final at = src.indexOf(_mark, setter);
      final mutated = src.replaceRange(at, at + _mark.length, '');
      expect(_Scan({...sources, 'class ProjectNotifier': mutated}).problems(),
          ['set shareToken does not mark the root changed']);
    });

    test('a new public mutable field, in the notifier or a subclass', () {
      final notifier = inject('class ProjectNotifier',
          '  bool _rootChanged = false;', '\n  bool bannerShown = false;');
      final shared = inject('class SharedProjectNotifier',
          '  bool _disposed = false;', '\n  String? notice;');
      expect(
          _Scan({
            ...sources,
            'class ProjectNotifier': notifier,
            'class SharedProjectNotifier': shared,
          }).problems(),
          unorderedEquals([
            'ProjectNotifier.bannerShown is a public mutable field',
            'SharedProjectNotifier.notice is a public mutable field',
          ]));
    });

    test('a root collection changed in place, in the notifier or a mixin', () {
      final notifier = inject(
          'class ProjectNotifier', '  void clear() {', ' members.clear();');
      final people = inject('mixin ProjectPeopleCrudMixin',
          'Future<List<Map<String, dynamic>>?> fetchPersonPolarstepsTrips(\n      int personId) async {',
          ' polarstepsOverlaySteps.add({});');
      expect(
          _Scan({
            ...sources,
            'class ProjectNotifier': notifier,
            'mixin ProjectPeopleCrudMixin': people,
          }).problems(),
          unorderedEquals([
            'members changed in place',
            'polarstepsOverlaySteps changed in place',
          ]));
    });
  });
}

/// The root fields of the declarations in [sources], read from source with
/// comments and string contents blanked out.
class _Scan {
  _Scan(Map<String, String> sources) {
    for (final e in sources.entries) {
      final code = _blank(e.value.replaceAll('\r\n', '\n'));
      final start = code.indexOf('${e.key} ');
      if (start < 0) throw StateError('${e.key} not found');
      final open = code.indexOf('{', start);
      _bodies[e.key.split(' ').last] =
          code.substring(open + 1, _matching(code, open));
    }
    _readMembers();
  }

  /// Class body by declaration name.
  final Map<String, String> _bodies = {};

  /// The private fields behind the notifier's setters.
  final Set<String> backingFields = {};

  /// Public mutable instance fields, by bare name.
  final Set<String> publicFields = {};

  final List<String> _problems = [];

  /// Each setter's body range in the notifier's body.
  final List<(int, int)> _setterBodies = [];

  /// Each of the notifier's private fields' declaration range in its body.
  final Map<String, (int, int)> _declarations = {};

  void _readMembers() {
    for (final MapEntry(key: cls, value: body) in _bodies.entries) {
      _members(body, (text, start, end, blockStart) {
        if (blockStart != null) {
          final setter = RegExp(r'\bset\s+(\w+)\s*\(')
              .firstMatch(body.substring(start, blockStart));
          if (setter == null || cls != 'ProjectNotifier') return;
          final name = setter.group(1)!;
          final block = body.substring(blockStart, end);
          if (!RegExp('(?<![\\w.])_$name\\s*=(?![=>])').hasMatch(block)) {
            _problems.add('set $name does not write _$name');
          }
          if (!block.contains(_mark)) {
            _problems.add('set $name does not mark the root changed');
          }
          backingFields.add('_$name');
          _setterBodies.add((blockStart, end));
          return;
        }
        final field = _fieldName(text);
        if (field == null) return;
        if (field.startsWith('_')) {
          if (cls == 'ProjectNotifier') _declarations[field] = (start, end);
          return;
        }
        final head = text.split(RegExp(r'=(?![=>])')).first;
        if (RegExp(r'\b(final|const)\b').hasMatch(head)) return;
        publicFields.add(field);
        if (!_testSeams.contains(field)) {
          _problems.add('$cls.$field is a public mutable field');
        }
      });
    }
  }

  /// Everything that changes root state without marking it.
  List<String> problems() {
    final out = [..._problems];
    for (final field in backingFields) {
      final f = RegExp.escape(field);
      final write = RegExp('(?:(?<![\\w.])|(?<=\\bthis\\.))$f\\s*'
          '(?:=(?![=>])|\\?\\?=|[-+*/~%|&^]=|\\+\\+|--)|(?:\\+\\+|--)$f\\b');
      for (final MapEntry(key: cls, value: body) in _bodies.entries) {
        for (final m in write.allMatches(body)) {
          bool within((int, int)? r) =>
              r != null && m.start >= r.$1 && m.start < r.$2;
          final allowed = cls == 'ProjectNotifier' &&
              (_setterBodies.any(within) || within(_declarations[field]));
          if (!allowed) out.add('$field written outside its setter');
        }
      }
    }
    // A collection changed in place: through the setter's name or the field.
    const mutators = 'add|addAll|insert|insertAll|remove|removeAt|removeLast|'
        'removeRange|removeWhere|retainWhere|clear|sort|shuffle|setAll|'
        'setRange|fillRange|replaceRange|putIfAbsent|update|updateAll';
    for (final field in backingFields) {
      final name = field.substring(1);
      final inPlace = RegExp('(?<![\\w.])(?:this\\.)?_?$name\\s*[!?]?\\s*'
          '(?:\\.\\s*(?:strava|polarsteps)\\s*)?\\.\\s*(?:$mutators)\\s*\\(|'
          '(?<![\\w.])_?$name\\s*!?\\s*\\[[^\\]]*\\]\\s*=(?![=>])');
      for (final body in _bodies.values) {
        if (inPlace.hasMatch(body)) out.add('$name changed in place');
      }
    }
    return out;
  }

  /// Splits [body] into members, calling [visit] with each member's text,
  /// its range in [body] and, for a block member, where its block starts.
  static void _members(String body,
      void Function(String text, int start, int end, int? blockStart) visit) {
    var i = 0;
    while (i < body.length) {
      var depth = 0;
      var sawAssign = false;
      final start = i;
      int? blockStart;
      for (; i < body.length; i++) {
        final c = body[i];
        // Not `<`: it is also less-than, and generics hold no `;`, `{` or `=`.
        if (c == '(' || c == '[') depth++;
        if (c == ')' || c == ']') depth--;
        if (depth == 0 && c == '=') sawAssign = true;
        if (c == '{') {
          final end = _matching(body, i);
          if (depth == 0 && !sawAssign) {
            blockStart = i;
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
      if (text.isNotEmpty) visit(text, start, i, blockStart);
    }
  }

  /// The declared name when [member] (ending in `;`) is an instance field.
  static String? _fieldName(String member) {
    if (!member.endsWith(';')) return null;
    var head = member.substring(0, member.length - 1);
    final arrow = head.indexOf('=>');
    final assign = RegExp(r'=(?![=>])').firstMatch(head)?.start ?? -1;
    if (arrow >= 0 && (assign < 0 || arrow < assign)) return null;
    if (assign >= 0) head = head.substring(0, assign);
    head = head.replaceAll(RegExp(r'@\w+(\([^)]*\))?'), ' ').trim();
    if (RegExp(r'\b(static|get|set|operator|factory|abstract|external)\b')
        .hasMatch(head)) {
      return null;
    }
    if (head.endsWith(')') || head.contains(':')) return null;
    return RegExp(r'(_?[A-Za-z]\w*)$').firstMatch(head)?.group(1);
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
  /// interpolated expressions kept.
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
