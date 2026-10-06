// Every listener reads only what it listens to (issue #294, unit U18 of
// docs/CLIENT_STATE_MAP_PLAN.md).
//
// Once the root stops re-notifying facet changes (U19), a widget rebuilt by
// the root that reads facet state shows that state as it was at the last root
// change: a filter badge that misses a tap, a chart that misses a selection.
// The source audit below finds every listener on a ProjectNotifier in lib/
// and fails when its builder reads a facet it does not listen to.
//
// **The rule.** A listener listens to the facets named in what it subscribes
// to: a `ListenableBuilder`/`AnimatedBuilder` to the facets in its
// `listenable`/`animation` (and to the root when that names a notifier); a
// `Consumer`, `Selector`, `context.select`, `context.watch` or `Provider.of`
// on a `…ProjectNotifier` type to the root only. Its builder reads facet F
// when, lexically, it
//  - names F's notifier getter or type (`itemsFacet`, `ItemsFacet`), or
//  - reads a notifier getter or value-returning method whose body reads F,
//    directly or through another one (P2-R1-2: a root getter that reads a
//    facet internally). That set is derived from the notifier's, its mixins'
//    and its two subclasses' sources, not listed by hand, so a getter added
//    later is covered.
// The builder is its own text, plus the bodies of the same-file private
// methods, getters and widget classes it calls (transitively), minus
//  - nested listeners (each is checked on its own),
//  - event callbacks (`onTap: () => …`, `onPressed: n.retry`): they run on
//    the event, not on the build, so they read the state current then,
//  - hand-offs (`elevation: n.elevationFacet`): a facet passed to a widget
//    that listens to it itself.
// For `context.watch`/`Provider.of` the builder is the rest of the enclosing
// block; for `context.select` and a `Selector`'s selector, the selector too.
//
// **Its limits.** It is lexical: it follows calls within a file, not into
// another file's widgets (those are audited where they are: map_panel.dart and
// the side panels by their own scope tests), and a facet reached through a
// variable of another name is not seen. A same-file helper that reads facet
// state deliberately on a root change can opt out with a
// `// root-listener-audit: allow — <reason>` line just above its declaration.

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart' show ApiException;
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/app_screen.dart';
import 'package:traxjourney_client/src/projects/elevation_chart.dart';
import 'package:traxjourney_client/src/projects/project_filters.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/view_screen.dart';
import 'package:traxjourney_client/src/shared/shared_project_screen.dart';

// ── Dart source, lexically ──────────────────────────────────────────────────

bool _isIdent(int c) =>
    (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) ||
    c == 95 || c == 36;

/// [src] with its comments and string contents blanked to spaces, newlines
/// and offsets kept. String interpolations stay: they are code.
String _codeOnly(String src) {
  final b = src.codeUnits.toList();
  final n = src.length;
  void blank(int from, int to) {
    for (var k = from; k < to && k < n; k++) {
      if (b[k] != 10) b[k] = 32;
    }
  }

  late int Function(int, {required bool raw}) string;

  // Code from [i] to the end, or to the `}` closing an interpolation.
  int code(int i, {required bool interpolation}) {
    var depth = 0;
    while (i < n) {
      final c = src[i];
      if (src.startsWith('//', i)) {
        final e = src.indexOf('\n', i);
        final end = e < 0 ? n : e;
        blank(i, end);
        i = end;
      } else if (src.startsWith('/*', i)) {
        final e = src.indexOf('*/', i + 2);
        final end = e < 0 ? n : e + 2;
        blank(i, end);
        i = end;
      } else if (c == "'" || c == '"') {
        i = string(i, raw: false);
      } else if (c == 'r' &&
          i + 1 < n &&
          (src[i + 1] == "'" || src[i + 1] == '"') &&
          (i == 0 || !_isIdent(src.codeUnitAt(i - 1)))) {
        i = string(i + 1, raw: true);
      } else {
        if (interpolation) {
          if (c == '{') depth++;
          if (c == '}') {
            if (depth == 0) return i + 1;
            depth--;
          }
        }
        i++;
      }
    }
    return i;
  }

  string = (int i, {required bool raw}) {
    final q = src[i];
    final close = src.startsWith(q * 3, i) ? q * 3 : q;
    var j = i + close.length;
    while (j < n) {
      if (src.startsWith(close, j)) return j + close.length;
      if (!raw && src[j] == r'\') {
        blank(j, j + 2);
        j += 2;
      } else if (!raw && src.startsWith(r'${', j)) {
        j = code(j + 2, interpolation: true);
      } else if (!raw && src[j] == r'$') {
        j++;
        while (j < n && _isIdent(src.codeUnitAt(j))) {
          j++;
        }
      } else if (close.length == 1 && src[j] == '\n') {
        return j;
      } else {
        blank(j, j + 1);
        j++;
      }
    }
    return j;
  };

  code(0, interpolation: false);
  return String.fromCharCodes(b);
}

const _opens = '([{';
const _closes = ')]}';

/// The index just past the bracket that closes the one at [open].
int _closing(String code, int open) {
  var depth = 0;
  for (var i = open; i < code.length; i++) {
    if (_opens.contains(code[i])) depth++;
    if (_closes.contains(code[i])) {
      depth--;
      if (depth == 0) return i + 1;
    }
  }
  return code.length;
}

/// The index just past the `>` that closes the type arguments at [open].
int _closingAngle(String code, int open) {
  var depth = 0;
  for (var i = open; i < code.length; i++) {
    if (code[i] == '<') depth++;
    if (code[i] == '>') {
      depth--;
      if (depth == 0) return i + 1;
    }
  }
  return code.length;
}

/// Where the expression starting at [from] ends: at the first `,`, `;` or
/// unmatched closing bracket outside brackets.
int _expressionEnd(String code, int from) {
  var depth = 0;
  for (var i = from; i < code.length; i++) {
    final c = code[i];
    if (_opens.contains(c)) depth++;
    if (_closes.contains(c)) {
      if (depth == 0) return i;
      depth--;
    }
    if ((c == ',' || c == ';') && depth == 0) return i;
  }
  return code.length;
}

/// The end of the block enclosing [from]: the `}` that closes it.
int _blockEnd(String code, int from) {
  var depth = 0;
  for (var i = from; i < code.length; i++) {
    if (_opens.contains(code[i])) depth++;
    if (_closes.contains(code[i])) {
      if (depth == 0) return i;
      depth--;
    }
  }
  return code.length;
}

int _skipSpace(String code, int i) {
  while (i < code.length && ' \t\r\n'.contains(code[i])) {
    i++;
  }
  return i;
}

/// The body after a declaration's parameters, or its name when it has none,
/// at [i]: `{ … }` or `=> …;`. Null for no body (abstract, a constructor's
/// initializer list, a call).
(int, int)? _bodyAt(String code, int i) {
  i = _skipSpace(code, i);
  for (final modifier in ['async*', 'async', 'sync*']) {
    if (code.startsWith(modifier, i)) {
      i = _skipSpace(code, i + modifier.length);
      break;
    }
  }
  if (code.startsWith('=>', i)) {
    var end = _expressionEnd(code, i + 2);
    // `=> a, b` cannot happen in a body; an arrow body ends at its `;`.
    while (end < code.length && code[end] == ',') {
      end = _expressionEnd(code, end + 1);
    }
    return (i, end);
  }
  if (i < code.length && code[i] == '{') return (i, _closing(code, i));
  return null;
}

// ── Facets ──────────────────────────────────────────────────────────────────

/// A facet's notifier getter (`itemsFacet`), writer (`itemsFacetWriter`) or
/// type (`ItemsFacet`). Group 1 names the facet.
final _facetToken = RegExp(
    r'\b(geo|selection|style|items|elevation|Geo|Selection|Style|Items|Elevation)Facet(?:Writer)?\b');

Set<String> _facetsIn(String text) => {
      for (final m in _facetToken.allMatches(text)) m.group(1)!.toLowerCase(),
    };

// ── What the notifier's members read ────────────────────────────────────────

/// A member declaration of a class body indented two spaces: its type (null
/// for none), `get` when a getter, its name, and what follows the name.
final _member = RegExp(
    r'^  (?:@\w+[ \t]+)*(?:(?:static|external|late|final|const|covariant)[ \t]+)*'
    r'(?:(\S[^\n=;]*?)[ \t]+)?(get[ \t]+)?([A-Za-z_]\w*)[ \t]*(\(|=>|\{)',
    multiLine: true);

/// The class or mixin bodies of [code] whose declaration matches [decl].
Iterable<String> _bodiesOf(String code, RegExp decl) sync* {
  for (final m in decl.allMatches(code)) {
    final open = code.indexOf('{', m.end);
    if (open < 0) continue;
    yield code.substring(open + 1, _closing(code, open) - 1);
  }
}

/// For each getter or value-returning method of [classBodies] that reads a
/// facet — itself, or through another such member — the facets it reads.
/// Members are keyed by name: one name's bodies across the classes are read
/// as one, which can only widen what a name counts as reading.
Map<String, Set<String>> _derivedReads(Iterable<String> classBodies) {
  final bodies = <String, StringBuffer>{};
  for (final body in classBodies) {
    for (final m in _member.allMatches(body)) {
      final type = m.group(1)?.trim();
      final isGetter = m.group(2) != null;
      final name = m.group(3)!;
      if (!isGetter) {
        // A method's value is a read; a void or asynchronous one is an action.
        if (type == null ||
            RegExp(r'^(void|Future|FutureOr|Stream)\b').hasMatch(type) ||
            const {'if', 'for', 'while', 'switch', 'return', 'class'}
                .contains(type)) {
          continue;
        }
      }
      var at = m.end - m.group(4)!.length;
      if (body[at] == '(') at = _closing(body, at);
      final span = _bodyAt(body, at);
      if (span == null) continue;
      (bodies[name] ??= StringBuffer()).write(body.substring(span.$1, span.$2));
    }
  }
  final reads = {
    for (final e in bodies.entries) e.key: _facetsIn(e.value.toString()),
  };
  final word = RegExp(r'\b[A-Za-z_]\w*\b');
  final calls = {
    for (final e in bodies.entries)
      e.key: {
        for (final w in word.allMatches(e.value.toString()))
          if (bodies.containsKey(w.group(0)) && w.group(0) != e.key)
            w.group(0)!,
      },
  };
  for (var changed = true; changed;) {
    changed = false;
    for (final e in calls.entries) {
      for (final callee in e.value) {
        final before = reads[e.key]!.length;
        reads[e.key]!.addAll(reads[callee]!);
        if (reads[e.key]!.length != before) changed = true;
      }
    }
  }
  reads.removeWhere((_, facets) => facets.isEmpty);
  return reads;
}

/// The notifier, its mixins and its two subclasses, as class bodies.
Iterable<String> _notifierBodies(Map<String, String> lib) sync* {
  final classes = {
    'lib/src/projects/project_notifier.dart':
        RegExp(r'^class ProjectNotifier\b', multiLine: true),
    'lib/src/projects/view_screen.dart':
        RegExp(r'^class ViewProjectNotifier\b', multiLine: true),
    'lib/src/shared/shared_project_screen.dart':
        RegExp(r'^class SharedProjectNotifier\b', multiLine: true),
  };
  for (final e in lib.entries) {
    final decl = classes[e.key] ??
        (RegExp(r'^lib/src/projects/project_\w+_mixin\.dart$').hasMatch(e.key)
            ? RegExp(r'^mixin \w+', multiLine: true)
            : null);
    if (decl != null) yield* _bodiesOf(_codeOnly(e.value), decl);
  }
}

// ── Listeners ───────────────────────────────────────────────────────────────

final _projectNotifierType = RegExp(r'\b\w*ProjectNotifier\b');

class _Listener {
  _Listener(this.kind, this.start, this.end, this.listens,
      {this.skip = const [], this.restOfBlock = false});

  final String kind;

  /// Whether the builder is the rest of the block after the call
  /// (`context.watch`, `Provider.of`), rather than the call's arguments.
  final bool restOfBlock;

  /// What the builder is: [start, end) of the code, less [skip].
  final int start;
  final int end;
  final List<(int, int)> skip;

  /// The facets it listens to; the root is implied.
  final Set<String> listens;
}

/// Every listener of [code] that listens to a ProjectNotifier or a facet of
/// one. [src] is the original source, for the declared types of the names a
/// `listenable` mentions.
List<_Listener> _listeners(String code) {
  final found = <_Listener>[];

  // Consumer<…ProjectNotifier…>( / Selector<…ProjectNotifier, …>(
  for (final m in RegExp(r'\b(Consumer\d?|Selector\d?)<').allMatches(code)) {
    final typeEnd = _closingAngle(code, m.end - 1);
    if (!_projectNotifierType.hasMatch(code.substring(m.end, typeEnd))) {
      continue;
    }
    final open = _skipSpace(code, typeEnd);
    if (open >= code.length || code[open] != '(') continue;
    found.add(_Listener(m.group(1)!, open, _closing(code, open), {}));
  }

  // context.select<…ProjectNotifier, …>(…) / context.watch<…>() /
  // Provider.of<…>(context) without `listen: false`.
  for (final m
      in RegExp(r'(\.select|\.watch|\bProvider\.of)<').allMatches(code)) {
    final typeEnd = _closingAngle(code, m.end - 1);
    if (!_projectNotifierType.hasMatch(code.substring(m.end, typeEnd))) {
      continue;
    }
    final open = _skipSpace(code, typeEnd);
    if (open >= code.length || code[open] != '(') continue;
    final close = _closing(code, open);
    if (m.group(1) == '.select') {
      found.add(_Listener('context.select', open, close, {}));
    } else if (!RegExp(r'listen\s*:\s*false')
        .hasMatch(code.substring(open, close))) {
      // Everything after it in the block rebuilds on the root.
      found.add(_Listener(
          m.group(1) == '.watch' ? 'context.watch' : 'Provider.of',
          close,
          _blockEnd(code, close),
          {},
          restOfBlock: true));
    }
  }

  // ListenableBuilder( listenable: … ) / AnimatedBuilder( animation: … )
  for (final m in RegExp(r'\b(ListenableBuilder|AnimatedBuilder)\s*\(')
      .allMatches(code)) {
    final open = m.end - 1;
    final close = _closing(code, open);
    final arg = RegExp(r'\b(?:listenable|animation)\s*:')
        .firstMatch(code.substring(open, close));
    if (arg == null) continue;
    final exprStart = open + arg.end;
    final exprEnd = _expressionEnd(code, exprStart);
    final expr = code.substring(exprStart, exprEnd);
    final facets = _facetsIn(expr);
    var root = false;
    // A bare name (not followed by `.`): what this file declares it as. A
    // ProjectNotifier type makes the listenable include the root; a facet
    // type, or a local set from a facet, that facet.
    for (final bare
        in RegExp(r'\b([A-Za-z_]\w*)\b(?!\s*[.(])').allMatches(expr)) {
      final name = RegExp.escape(bare.group(1)!);
      if (RegExp('\\b\\w*ProjectNotifier\\??\\s+(?:get\\s+)?$name\\b')
          .hasMatch(code)) {
        root = true;
      }
      for (final d in RegExp(
              '\\b\\w*Facet\\??\\s+$name\\b|\\b(?:final|var)\\s+$name\\s*=[^;]*;')
          .allMatches(code)) {
        facets.addAll(_facetsIn(d.group(0)!));
      }
    }
    if (facets.isEmpty && !root) continue;
    found.add(_Listener(m.group(1)!, open, close, facets,
        skip: [(exprStart, exprEnd)]));
  }
  return found;
}

/// What a builder spanning [start, end) of [code] reads once nested
/// listeners, event callbacks and hand-offs are taken out: the code left, as
/// a string, with the bodies of the same-file helpers it calls appended.
String _builderText(String code, String src, int start, int end,
    List<(int, int)> skip, List<_Listener> all, Set<String> followed) {
  final cut = List<bool>.filled(end - start, false);
  void drop(int from, int to) {
    for (var i = from < start ? start : from; i < to && i < end; i++) {
      cut[i - start] = true;
    }
  }

  for (final s in skip) {
    drop(s.$1, s.$2);
  }
  // Nested listeners: each is checked on its own.
  for (final l in all) {
    if (!l.restOfBlock && l.start > start && l.end <= end) {
      drop(l.start, l.end);
    }
  }
  final region = code.substring(start, end);
  // Event callbacks: a closure or a tear-off passed to an `on…:` argument.
  for (final m in RegExp(r'\bon[A-Z]\w*\s*:\s*').allMatches(region)) {
    final valueStart = start + m.end;
    final value = code.substring(valueStart, end);
    final closure = RegExp(r'^\(').hasMatch(value) &&
        _bodyAt(code, _closing(code, valueStart)) != null;
    final tearOff = RegExp(r'^[A-Za-z_][\w.]*\s*[,)\]}]').hasMatch(value);
    if (closure || tearOff) drop(valueStart, _expressionEnd(code, valueStart));
  }
  // Hand-offs: an argument that is exactly a facet.
  for (final m in RegExp(r'\b\w+\s*:\s*((?:[A-Za-z_]\w*\.)*\w+Facet)\s*(?=[,)])')
      .allMatches(region)) {
    final facet = m.group(1)!;
    final at = start + m.start + m.group(0)!.lastIndexOf(facet);
    drop(at, at + facet.length);
  }

  final text = StringBuffer();
  for (var i = start; i < end; i++) {
    text.write(cut[i - start] ? ' ' : code[i]);
  }
  final own = text.toString();

  // Same-file helpers it calls: private methods, getters and widget classes.
  bool allowed(int declAt) {
    final lineStart = src.lastIndexOf('\n', declAt) + 1;
    var from = lineStart;
    for (var k = 0; k < 3 && from > 0; k++) {
      from = src.lastIndexOf('\n', from - 2) + 1;
    }
    return src
        .substring(from, lineStart)
        .contains('root-listener-audit: allow');
  }

  for (final m in RegExp(r'\b(_\w+)\b').allMatches(own)) {
    final name = m.group(1)!;
    if (!followed.add(name)) continue;
    final escaped = RegExp.escape(name);
    if (RegExp(r'^_[A-Z]').hasMatch(name)) {
      // A private widget: its build, or its State's.
      for (final decl in [
        RegExp('^class $escaped\\b', multiLine: true),
        RegExp('^class \\w+ extends State<$escaped>', multiLine: true),
      ]) {
        for (final c in decl.allMatches(code)) {
          if (allowed(c.start)) continue;
          final open = code.indexOf('{', c.end);
          if (open < 0) continue;
          final classEnd = _closing(code, open);
          final build = RegExp(r'\bWidget\s+build\s*\([^)]*\)')
              .firstMatch(code.substring(open, classEnd));
          if (build == null) continue;
          final body = _bodyAt(code, open + build.end);
          if (body == null) continue;
          text.write('\n');
          text.write(_builderText(
              code, src, body.$1, body.$2, const [], all, followed));
        }
      }
    } else {
      // A private method or getter declared in this file.
      for (final d in RegExp(
              '^[ \\t]*(?:[\\w<>?,\\[\\]() ]+[ \\t]+)?(?:get[ \\t]+)?$escaped[ \\t]*(\\(|=>|\\{)',
              multiLine: true)
          .allMatches(code)) {
        var at = d.end - d.group(1)!.length;
        if (code[at] == '(') at = _closing(code, at);
        final body = _bodyAt(code, at);
        if (body == null || allowed(d.start)) continue;
        text.write('\n');
        text.write(_builderText(
            code, src, body.$1, body.$2, const [], all, followed));
      }
    }
  }
  return text.toString();
}

/// Each listener of [src] (at [path]) whose builder reads a facet it does not
/// listen to, as `path:line: kind reads …`.
List<String> _violations(
    String path, String src, Map<String, Set<String>> derived) {
  final code = _codeOnly(src);
  final listeners = _listeners(code);
  final out = <String>[];
  for (final l in listeners) {
    final text = _builderText(
        code, src, l.start, l.end, l.skip, listeners, <String>{});
    final reads = <String, Set<String>>{};
    for (final f in _facetsIn(text)) {
      (reads[f] ??= {}).add('${f}Facet');
    }
    for (final m in RegExp(r'\.([A-Za-z_]\w*)\b').allMatches(text)) {
      final name = m.group(1)!;
      for (final f in derived[name] ?? const <String>{}) {
        (reads[f] ??= {}).add(name);
      }
    }
    final unheard = reads.keys.toSet().difference(l.listens);
    if (unheard.isEmpty) continue;
    final line = '\n'.allMatches(src.substring(0, l.start)).length + 1;
    out.add('$path:$line: ${l.kind} '
        '(listens to ${l.listens.isEmpty ? 'the root' : l.listens.join(', ')}) '
        'reads ${[for (final f in unheard) '$f via ${reads[f]!.join('/')}'].join('; ')}');
  }
  return out;
}

Map<String, String> _lib() => {
      for (final f in Directory('lib').listSync(recursive: true))
        if (f is File && f.path.endsWith('.dart'))
          f.path.replaceAll(r'\', '/'): f.readAsStringSync(),
    };

List<String> _libViolations(Map<String, String> lib) {
  final derived = _derivedReads(_notifierBodies(lib));
  return [
    for (final e in lib.entries)
      if (!e.key.startsWith('lib/src/projects/facets/'))
        ..._violations(e.key, e.value, derived),
  ];
}

// ── Screen harnesses ────────────────────────────────────────────────────────

class _FakeProjectService extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': <dynamic>[],
        'people': <dynamic>[],
        'groups': <dynamic>[],
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier() : super(_FakeProjectService());

  @override
  bool get loadOwnerExtras => false;
}

AuthNotifier _signedIn() => AuthNotifier(AuthService())
  ..updateUser(const {
    'id': 'user-1',
    'email': 'a@x.com',
    'display_name': 'A',
    'auth_provider': 'local',
  });

/// Pumps AppScreen on an empty trip and waits for its load.
Future<ProjectNotifier> _pumpAppScreen(WidgetTester tester) async {
  final notifier = _TestProjectNotifier();
  final router = GoRouter(
    initialLocation: '/app?project=Trip',
    routes: [
      GoRoute(
        path: '/app',
        builder: (context, state) => AppScreen(
            projectName: state.uri.queryParameters['project'] ?? ''),
      ),
    ],
  );
  await tester.pumpWidget(MultiProvider(
    providers: [
      ChangeNotifierProvider<AuthNotifier>.value(value: _signedIn()),
      ChangeNotifierProvider<ProjectNotifier>.value(value: notifier),
    ],
    child: MaterialApp.router(routerConfig: router),
  ));
  await tester.pump();
  // AppScreen mounts a real map that never quiesces: no pumpAndSettle.
  for (var i = 0; i < 20 && notifier.isLoading; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
  await tester.pump();
  return notifier;
}

IconButton _buttonFor(WidgetTester tester, String tooltip) =>
    tester.widget<IconButton>(find.ancestor(
        of: find.byTooltip(tooltip), matching: find.byType(IconButton)).first);

Badge _badgeOf(WidgetTester tester, String tooltip) => tester.widget<Badge>(
    find.descendant(
        of: find.ancestor(
            of: find.byTooltip(tooltip), matching: find.byType(IconButton)),
        matching: find.byType(Badge)));

ElevationChart _chart(WidgetTester tester) =>
    tester.widget<ElevationChart>(find.byType(ElevationChart));

/// Tells only [n]'s selection facet's listeners: never the root's.
void _flushSelection(ProjectNotifier n) => n.selectionFacetWriter.flush();

void main() {
  group('source audit', () {
    test('no listener in lib/ reads a facet it does not listen to', () {
      expect(_libViolations(_lib()), isEmpty,
          reason: 'listen to each facet a builder reads (ListenableBuilder on '
              'the facets), or read only root state from a root listener');
    });

    test('the audit reads the notifier and finds the listeners it guards', () {
      final lib = _lib();
      final derived = _derivedReads(_notifierBodies(lib));
      // A getter of the root that reads facets internally (P2-R1-2).
      expect(derived['activeDayKey'], containsAll(['items']));
      expect(derived['geoFacet'], {'geo'});
      // Root state is not derived.
      for (final root in ['isLoading', 'error', 'projectName', 'isViewer']) {
        expect(derived, isNot(contains(root)), reason: root);
      }
      // The screens' listeners are found, so the audit is not vacuous.
      int count(String path) => _listeners(_codeOnly(lib[path]!)).length;
      expect(count('lib/src/projects/app_screen.dart'), greaterThanOrEqualTo(12));
      expect(count('lib/src/projects/view_screen.dart'), greaterThanOrEqualTo(9));
      expect(count('lib/src/shared/shared_project_screen.dart'),
          greaterThanOrEqualTo(3));
      expect(count('lib/src/projects/people_screen.dart'), 2);
      expect(count('lib/src/projects/travel_companions_section.dart'), 1);
      expect(count('lib/src/projects/project_stats_screen.dart'), 2);
    });

    group('fails on', () {
      // A stand-in notifier: one root field, one getter reading a facet
      // through another, one reading nothing.
      const notifierSrc = '''
class ProjectNotifier extends ChangeNotifier {
  bool isLoading = false;
  ItemsFacet get itemsFacet => itemsFacetWriter.facet;
  bool get hasContent => _countItems() > 0;
  int _countItems() => itemsFacet.items.length;
  String? get title => ref?.name;
  void reload() { itemsFacet.items; }
}
''';
      final derived = _derivedReads(
          _bodiesOf(notifierSrc, RegExp(r'^class ProjectNotifier\b', multiLine: true)));

      List<String> scan(String body) => _violations('w.dart', '''
class W extends StatelessWidget {
  final ProjectNotifier notifier;
  Widget build(BuildContext context) {
    $body
  }
  Widget _helper(ProjectNotifier n) => Text('\${n.selectionFacet.selectedDay}');
  // root-listener-audit: allow — test
  Widget _allowed(ProjectNotifier n) => Text('\${n.itemsFacet.items}');
}
''', derived);

      test('a Consumer reading a facet', () {
        expect(scan('''return Consumer<ProjectNotifier>(
            builder: (_, n, __) => Text('\${n.itemsFacet.items.length}'));'''),
            hasLength(1));
      });
      test('a root getter that reads a facet internally (P2-R1-2)', () {
        expect(scan('''return Consumer<ProjectNotifier>(
            builder: (_, n, __) => Text('\${n.hasContent}'));'''),
            [contains('items via hasContent')]);
      });
      test('a Selector reading a facet in its selector', () {
        expect(scan('''return Selector<ProjectNotifier, bool>(
            selector: (_, n) => n.itemsFacet.items.isEmpty,
            builder: (_, empty, __) => Text('\$empty'));'''), hasLength(1));
      });
      test('context.select and context.watch reading a facet', () {
        expect(scan('''final e = context.select<ProjectNotifier, bool>(
            (n) => n.selectionFacet.showJournals);
            return Text('\$e');'''), hasLength(1));
        expect(scan('''final n = context.watch<ProjectNotifier>();
            return Text('\${n.styleFacet.trackWidth}');'''), hasLength(1));
      });
      test('a facet listener reading a facet it does not listen to', () {
        expect(scan('''return ListenableBuilder(
            listenable: notifier.selectionFacet,
            builder: (_, __) => Text('\${notifier.itemsFacet.items}'));'''),
            [contains('reads items')]);
      });
      test('a root listener calling a same-file helper that reads a facet', () {
        expect(scan('''return Consumer<ProjectNotifier>(
            builder: (_, n, __) => _helper(n));'''), hasLength(1));
      });
      test('a listenable held in a local, reading another facet', () {
        expect(scan('''final items = notifier.itemsFacet;
            return ListenableBuilder(listenable: items,
            builder: (_, __) => Text('\${notifier.styleFacet.trackWidth}'));'''),
            [contains('(listens to items) reads style')]);
      });
      test('an AnimatedBuilder on the notifier reading a facet', () {
        expect(scan('''return AnimatedBuilder(animation: notifier,
            builder: (_, __) => Text('\${notifier.geoFacet.geo}'));'''),
            hasLength(1));
      });

      test('— and passes what is right', () {
        expect(
            scan('''
            final n = context.select<ProjectNotifier, ProjectNotifier>((n) => n);
            final t = context.select<ProjectNotifier, String?>((n) => n.title);
            return Column(children: [
              Consumer<ProjectNotifier>(
                builder: (_, n, __) => Panel(
                  notifier: n,
                  loading: n.isLoading,
                  chart: Chart(elevation: n.elevationFacet),
                  onTap: () => n.itemsFacet.items.clear(),
                  onRetry: n.reload,
                  child: _allowed(n),
                  list: ListenableBuilder(
                    listenable: n.itemsFacet,
                    builder: (_, __) => Text('\${n.itemsFacet.items}'),
                  ),
                ),
              ),
              ListenableBuilder(
                listenable: Listenable.merge([notifier, notifier.selectionFacet]),
                builder: (_, __) => Text(
                    '\${notifier.isLoading} \${notifier.selectionFacet.selectedDay}'),
              ),
            ]);'''),
            isEmpty);
      });
    });
  });

  group('the screens follow the facets', () {
    setUp(() => SharedPreferences.setMockInitialValues({}));

    testWidgets(
        "AppScreen's filter button follows a filter toggle and the first "
        'activity added to an empty trip, with no root notify', (tester) async {
      final n = await _pumpAppScreen(tester);
      // The load fills in today as a day with no sleeping data, which is
      // something to filter on ('No data'); take it back out, telling the
      // items facet alone.
      expect(_buttonFor(tester, 'Filter').onPressed, isNotNull);
      n.itemsFacetWriter.setDayMeta({});
      n.itemsFacetWriter.flush();
      await tester.pump();
      expect(_buttonFor(tester, 'Filter').onPressed, isNull,
          reason: 'an empty trip has nothing to filter');
      expect(_badgeOf(tester, 'Filter').isLabelVisible, isFalse);

      n.itemsFacetWriter.setActivities([
        {'id': 1, 'type': 'Hike', 'start_date_local': '2024-04-01T08:00:00'},
      ]);
      n.itemsFacetWriter.flush();
      await tester.pump();
      expect(_buttonFor(tester, 'Filter').onPressed, isNotNull,
          reason: 'the first activity gives the trip a type to filter on');

      n.selectionFacetWriter.setFilters(
          const ProjectFilters(activityTypes: {'hike'}), const {});
      _flushSelection(n);
      await tester.pump();
      expect(_badgeOf(tester, 'Filter').isLabelVisible, isTrue);
      expect(
          find.descendant(
              of: find.byType(Badge), matching: find.text('1')),
          findsOneWidget);
    });

    testWidgets(
        "AppScreen's elevation chart follows the elevation facet and a "
        'selection, wide and narrow', (tester) async {
      final n = await _pumpAppScreen(tester);
      expect(_chart(tester).elevation, same(n.elevationFacet));

      n.selectionFacetWriter.selectActivity(7);
      _flushSelection(n);
      await tester.pump();
      expect(_chart(tester).selectedActivityId, 7);

      tester.view.physicalSize = const Size(700, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      await tester.pump();
      expect(_chart(tester).elevation, same(n.elevationFacet));
      n.selectionFacetWriter.selectActivity(8);
      _flushSelection(n);
      await tester.pump();
      expect(_chart(tester).selectedActivityId, 8);
    });

    testWidgets(
        'AppScreen still shows root changes: loading, and the Polarsteps '
        'overlay the map panel reads through its parent', (tester) async {
      tester.view.physicalSize = const Size(700, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final n = await _pumpAppScreen(tester);
      expect(_buttonFor(tester, 'Statistics').onPressed, isNotNull);

      n.isLoading = true;
      n.notifyListeners();
      await tester.pump();
      expect(_buttonFor(tester, 'Statistics').onPressed, isNull);
      n.isLoading = false;
      n.notifyListeners();
      await tester.pump();

      n.polarstepsOverlaySteps = [
        {'lat': 48.0, 'lon': 2.0, 'date': '2024-04-01', 'name': 'Paris'},
      ];
      n.polarstepsOverlayLabel = 'Alice · Asia 2024';
      n.notifyListeners();
      await tester.pump();
      expect(find.text('Alice · Asia 2024'), findsOneWidget);
    });

    testWidgets(
        "ViewScreen's tag filter, error and chart follow the facets and the "
        'root', (tester) async {
      // ViewScreen loads over the network, which fails in tests (see
      // view_screen_test.dart): only that ApiException is expected.
      final previousReporter = reportTestException;
      reportTestException = (details, description) {
        if (details.exception is! ApiException) {
          previousReporter(details, description);
        }
      };

      await tester.pumpWidget(MaterialApp(
        home: ChangeNotifierProvider<AuthNotifier>.value(
          value: _signedIn(),
          child: const ViewScreen(projectName: 'Trip'),
        ),
      ));
      await tester.pump();
      final n = Provider.of<ViewProjectNotifier>(
          tester.element(find.byType(Scaffold).first),
          listen: false);

      // The tag filter: the trip's tags (items), then a ticked one
      // (selection), each told to its facet alone.
      expect(_buttonFor(tester, 'Filter by tag').onPressed, isNull);
      n.itemsFacetWriter.setDayMeta({
        '2024-04-01': {
          'tags': ['beach'],
        },
      });
      n.itemsFacetWriter.flush();
      await tester.pump();
      expect(_buttonFor(tester, 'Filter by tag').onPressed, isNotNull);
      n.selectionFacetWriter
          .setFilters(const ProjectFilters(tags: {'beach'}), const {});
      _flushSelection(n);
      await tester.pump();
      expect(_badgeOf(tester, 'Filter by tag').isLabelVisible, isTrue);

      // The error, a root change, while the meta is not loaded (the failed
      // load has already marked it loaded).
      n.isMetaLoaded = false;
      n.error = 'Could not load this trip';
      n.notifyListeners();
      await tester.pump();
      expect(find.text('Could not load this trip'), findsOneWidget);

      // Meta and elevation loaded (root): the chart, on the elevation facet,
      // following a selection told to the selection facet alone.
      n.isMetaLoaded = true;
      n.isElevationLoaded = true;
      n.notifyListeners();
      await tester.pump();
      expect(_chart(tester).elevation, same(n.elevationFacet));
      n.selectionFacetWriter.selectActivity(3);
      _flushSelection(n);
      await tester.pump();
      expect(_chart(tester).selectedActivityId, 3);

      // Flush the failed load's retry timers before teardown.
      await tester.pump(const Duration(seconds: 20));
      reportTestException = previousReporter;
    });

    testWidgets(
        "SharedProjectScreen's chart and list follow the facets and its body "
        'the root', (tester) async {
      final previousReporter = reportTestException;
      reportTestException = (details, description) {
        if (details.exception is! ApiException) {
          previousReporter(details, description);
        }
      };

      await tester.pumpWidget(MaterialApp(
        home: ChangeNotifierProvider<AuthNotifier>.value(
          value: AuthNotifier(AuthService()),
          child: const SharedProjectScreen(token: 'tok'),
        ),
      ));
      // The anonymous id is read before the notifier is made.
      for (var i = 0; i < 10 && find.byType(ViewOnlyBanner).evaluate().isEmpty;
          i++) {
        await tester.runAsync(
            () => Future<void>.delayed(const Duration(milliseconds: 10)));
        await tester.pump();
      }
      final n = Provider.of<SharedProjectNotifier>(
          tester.element(find.byType(ViewOnlyBanner)),
          listen: false);

      n.isMetaLoaded = true;
      n.isElevationLoaded = true;
      n.notifyListeners();
      await tester.pump();
      expect(_chart(tester).elevation, same(n.elevationFacet));

      n.itemsFacetWriter.setActivities([
        {'id': 4, 'name': 'Coastal walk', 'type': 'Hike', 'distance': 1000},
      ]);
      n.itemsFacetWriter.flush();
      await tester.pump();
      expect(find.text('Coastal walk'), findsOneWidget);

      n.selectionFacetWriter.selectActivity(4);
      _flushSelection(n);
      await tester.pump();
      expect(_chart(tester).selectedActivityId, 4);
      expect(
          tester
              .widget<ListTile>(find.ancestor(
                  of: find.text('Coastal walk'),
                  matching: find.byType(ListTile)))
              .selected,
          isTrue);

      await tester.pump(const Duration(seconds: 20));
      reportTestException = previousReporter;
    });
  });
}
