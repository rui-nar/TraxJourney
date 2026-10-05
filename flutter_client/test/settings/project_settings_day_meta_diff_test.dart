// Issue #397: day-meta saves send only the days that changed, to
// PATCH /day-meta, and adopt the merged map the server returns.
//
// The settings-screen tests drive the real screen and read the request bodies
// off the wire. The notifier tests cover saveDayMeta's own contract: adopting
// the response, the reload on failure, and the 405 fallback to the whole-map
// PUT for a server that predates the PATCH.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/project_settings_screen.dart';

import '../helpers/signed_in.dart';

/// Every day-meta write during a test: the method, the path and the decoded
/// body.
late List<({String method, String path, Map<String, dynamic> body})>
    dayMetaWrites;

/// When set, the PATCH answers this status instead of 200.
late int? patchStatus;

/// What a 200 PATCH returns as the merged map.
late Map<String, dynamic> patchResponse;

/// What GET /meta answers for day-meta.
late Map<String, dynamic> metaDayMeta;

/// When set, a PATCH is answered only once this completes.
Completer<void>? patchGate;

ApiClient _recordingApi() => ApiClient(
      httpClient: MockClient((req) async {
        if (req.url.path.endsWith('/day-meta') &&
            (req.method == 'PATCH' || req.method == 'PUT')) {
          dayMetaWrites.add((
            method: req.method,
            path: req.url.path,
            body: jsonDecode(req.body) as Map<String, dynamic>,
          ));
          if (req.method == 'PATCH' && patchGate != null) {
            await patchGate!.future;
          }
          if (req.method == 'PATCH' && patchStatus != null) {
            return http.Response('{"detail":"nope"}', patchStatus!);
          }
          if (req.method == 'PATCH') {
            return http.Response(jsonEncode({'day_meta': patchResponse}), 200);
          }
          return http.Response('', 204);
        }
        if (req.method == 'GET' && req.url.path.endsWith('/meta')) {
          return http.Response(
              jsonEncode({
                'day_meta': metaDayMeta,
                'sleeping_options': ['Hotel'],
                'counters': [],
              }),
              200);
        }
        if (req.method == 'GET' && req.url.path.endsWith('/content-days')) {
          return http.Response(jsonEncode({'days': <String>[]}), 200);
        }
        if (req.method == 'GET' && req.url.path.endsWith('/polarsteps/trips')) {
          return http.Response('[]', 200);
        }
        return http.Response('{}', 200);
      }),
    );

Map<String, Map<String, dynamic>> _days(Map<String, List<String>> tagsByDay) => {
      for (final e in tagsByDay.entries)
        e.key: <String, dynamic>{'note': 'n ${e.key}', 'tags': e.value},
    };

ProjectNotifier _notifier(Map<String, Map<String, dynamic>> dayMeta,
        {String? tripEnd}) =>
    ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip')
      ..tripStart = '2026-06-01'
      ..tripEnd = tripEnd
      ..dayMeta = dayMeta;

Future<void> _frames(WidgetTester tester, [int n = 8]) async {
  for (var i = 0; i < n; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

Future<void> _pumpSettings(WidgetTester tester, ProjectNotifier n) async {
  tester.view.physicalSize = const Size(520, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final router = GoRouter(routes: [
    GoRoute(
      path: '/',
      builder: (_, __) => const Scaffold(body: Text('home')),
      routes: [
        GoRoute(
          path: 'settings',
          builder: (_, __) => const ProjectSettingsScreen(projectName: 'Trip'),
        ),
      ],
    ),
  ]);
  await tester.pumpWidget(ChangeNotifierProvider<ProjectNotifier>.value(
    value: n,
    child: MaterialApp.router(routerConfig: router),
  ));
  router.go('/settings');
  await _frames(tester);
}

Future<void> _tapSave(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Save'));
  await _frames(tester);
}

String _ymd(DateTime d) => '${d.year.toString().padLeft(4, '0')}-'
    '${d.month.toString().padLeft(2, '0')}-'
    '${d.day.toString().padLeft(2, '0')}';

void main() {
  late ApiClient realApi;

  setUp(() {
    dayMetaWrites = [];
    patchStatus = null;
    patchGate = null;
    patchResponse = {};
    metaDayMeta = {};
    realApi = api;
    api = _recordingApi();
  });
  tearDown(() => api = realApi);

  group('settings screen', () {
    testWidgets('a colour-only save sends no day-meta request', (tester) async {
      final n = _notifier(_days({
        '2026-06-13': ['a'],
        '2026-06-14': [],
      }));
      await _pumpSettings(tester, n);
      await tester.tap(find.byIcon(Icons.polyline));
      await _frames(tester);
      await tester.tap(find.byType(Switch).first);
      await _frames(tester);

      await _tapSave(tester);

      expect(find.text('home'), findsOneWidget);
      expect(dayMetaWrites, isEmpty);
    });

    testWidgets('a tag rename sends exactly the renamed days', (tester) async {
      // "old" is on three days and "keep" on two, so "old" is listed first.
      final n = _notifier(_days({
        '2026-06-13': ['old', 'keep'],
        '2026-06-14': ['keep'],
        '2026-06-15': ['old'],
        '2026-06-16': ['old'],
      }));
      await _pumpSettings(tester, n);
      await tester.tap(find.byIcon(Icons.label_outlined));
      await _frames(tester);
      await tester.tap(find.byTooltip('Rename').first);
      await _frames(tester);
      await tester.enterText(
          find.descendant(
              of: find.byType(AlertDialog), matching: find.byType(TextField)),
          'new');
      await tester.tap(find.descendant(
          of: find.byType(AlertDialog), matching: find.text('Rename')));
      await _frames(tester);

      await _tapSave(tester);

      expect(dayMetaWrites, hasLength(1));
      final w = dayMetaWrites.single;
      expect(w.method, 'PATCH');
      expect(w.body['delete'], isEmpty);
      final days = w.body['days'] as Map<String, dynamic>;
      expect(days.keys.toSet(), {'2026-06-13', '2026-06-15', '2026-06-16'});
      expect((days['2026-06-13'] as Map)['tags'], ['new', 'keep']);
    });

    testWidgets('a tag removal sends exactly the days it touched',
        (tester) async {
      final n = _notifier(_days({
        '2026-06-13': ['old', 'keep'],
        '2026-06-14': ['keep'],
        '2026-06-15': ['old'],
        '2026-06-16': ['old'],
      }));
      await _pumpSettings(tester, n);
      await tester.tap(find.byIcon(Icons.label_outlined));
      await _frames(tester);
      await tester.tap(find.byTooltip('Delete from all days').first);
      await _frames(tester);

      await _tapSave(tester);

      expect(dayMetaWrites, hasLength(1));
      final w = dayMetaWrites.single;
      expect(w.method, 'PATCH');
      expect(w.body['delete'], isEmpty);
      final days = w.body['days'] as Map<String, dynamic>;
      expect(days.keys.toSet(), {'2026-06-13', '2026-06-15', '2026-06-16'});
      expect((days['2026-06-13'] as Map)['tags'], ['keep']);
    });

    testWidgets('trip-end pruning sends the pruned days in delete',
        (tester) async {
      final n = _notifier(
        _days({
          '2026-06-13': [],
          '2026-06-14': [],
          '2026-06-15': [],
          '2026-06-16': [],
        }),
        tripEnd: '2026-06-14',
      );
      await _pumpSettings(tester, n);
      await _tapSave(tester);
      await tester.tap(find.text('Delete'));
      await _frames(tester);

      expect(dayMetaWrites, hasLength(1));
      expect(dayMetaWrites.single.body['delete'], ['2026-06-15', '2026-06-16']);
      expect(dayMetaWrites.single.body['days'], isEmpty);
    });
  });

  group('saveDayMeta', () {
    test('sends only the given days and the delete list', () async {
      final n = _notifier(_days({'2026-06-13': [], '2026-06-14': []}));
      patchResponse = _days({'2026-06-13': [], '2026-06-14': []});
      await n.saveDayMeta(days: {
        '2026-06-13': {'note': 'x'}
      }, delete: [
        '2026-06-14'
      ]);

      expect(dayMetaWrites.single.method, 'PATCH');
      expect(dayMetaWrites.single.body, {
        'days': {
          '2026-06-13': {'note': 'x'}
        },
        'delete': ['2026-06-14'],
      });
    });

    test('sends nothing when there is nothing to send', () async {
      final n = _notifier({});
      await n.saveDayMeta();
      expect(dayMetaWrites, isEmpty);
    });

    test('a day another device added arrives from the response', () async {
      final n = _notifier(_days({'2026-06-13': []}));
      patchResponse = {
        '2026-06-13': {'note': 'mine'},
        '2026-06-12': {'note': 'from the other phone'},
      };
      await n.saveDayMeta(days: {
        '2026-06-13': {'note': 'mine'}
      });

      expect(n.dayMeta['2026-06-12']?['note'], 'from the other phone');
      expect(n.dayMeta['2026-06-13']?['note'], 'mine');
    });

    test('saving a note keeps the carousel tiles up to today (R1-2)', () async {
      final today = DateTime.now();
      final twoAgo = today.subtract(const Duration(days: 2));
      final n = ProjectNotifier(ProjectService())
        ..ref = const ProjectRef(name: 'Trip')
        ..tripStart = _ymd(twoAgo)
        ..activities = [
          {'start_date_local': '${_ymd(twoAgo)}T08:00:00'}
        ]
        ..dayMeta = {_ymd(twoAgo): <String, dynamic>{}};
      // The server stores only the one day; the gap days live in memory.
      patchResponse = {
        _ymd(twoAgo): {'note': 'hi'}
      };
      await n.saveDayMeta(days: {
        _ymd(twoAgo): {'note': 'hi'}
      });

      expect(n.dayMeta.keys, contains(_ymd(today)));
      expect(n.dayMeta.keys,
          contains(_ymd(today.subtract(const Duration(days: 1)))));
      expect(n.dayMeta[_ymd(twoAgo)]?['note'], 'hi');
    });

    test('a failed PATCH reloads day-meta and sets the error', () async {
      final n = _notifier(_days({'2026-06-13': []}));
      patchStatus = 500;
      metaDayMeta = {
        '2026-06-13': {'note': 'server copy'}
      };
      await n.saveDayMeta(days: {
        '2026-06-13': {'note': 'optimistic'}
      });

      expect(n.dayMeta['2026-06-13']?['note'], 'server copy');
      expect(n.error, isNotNull);
      expect(dayMetaWrites.where((w) => w.method == 'PUT'), isEmpty);
    });

    test('a 405 sends one whole-map PUT and the note stays (R3-2)', () async {
      final n = _notifier(_days({'2026-06-13': [], '2026-06-14': []}));
      patchStatus = 405;
      await n.saveDayMeta(days: {
        '2026-06-13': {'note': 'kept'}
      }, delete: [
        '2026-06-14'
      ]);

      final puts = dayMetaWrites.where((w) => w.method == 'PUT').toList();
      expect(puts, hasLength(1));
      final sent = puts.single.body['day_meta'] as Map<String, dynamic>;
      expect(sent['2026-06-13'], {'note': 'kept'});
      expect(sent.keys, isNot(contains('2026-06-14')));
      expect(n.dayMeta['2026-06-13']?['note'], 'kept');
      expect(n.error, isNull);
    });

    test(
        "a 405 after another trip loaded sends A's save to A, never B's "
        'days (I1-R1-2, I1-R2-1)', () async {
      api.setToken(fakeJwt(sub: 1)); // one account throughout (I1-R3-2)
      final n = _notifier(_days({'2026-06-13': []}));
      patchStatus = 405;
      patchGate = Completer<void>();
      final save = n.saveDayMeta(days: {
        '2026-06-13': {'note': 'trip A'}
      });
      await Future<void>.delayed(Duration.zero);
      // What load() does synchronously, then trip B's /meta lands.
      n
        ..ref = const ProjectRef(name: 'Other')
        ..dayMeta = _days({'2026-07-01': [], '2026-07-02': []});
      patchGate!.complete();
      await save;

      // The trip switch must not discard A's save: the PUT carries the map
      // captured before the await, to A's path.
      final puts = dayMetaWrites.where((w) => w.method == 'PUT').toList();
      expect(puts, hasLength(1));
      expect(puts.single.path, endsWith('/projects/Trip/day-meta'));
      final sent = puts.single.body['day_meta'] as Map<String, dynamic>;
      expect(sent['2026-06-13'], {'note': 'trip A'});
      expect(sent.keys, isNot(contains('2026-07-01')));
      expect(sent.keys, isNot(contains('2026-07-02')));
      // B's state is left alone.
      expect(n.dayMeta.keys, containsAll(['2026-07-01', '2026-07-02']));
      expect(n.dayMeta.keys, isNot(contains('2026-06-13')));
    });

    test("a 405 after another account signed in sends no PUT (I1-R3-2)",
        () async {
      api.setToken(fakeJwt(sub: 1));
      final n = _notifier(_days({'2026-06-13': []}));
      patchStatus = 405;
      patchGate = Completer<void>();
      final save = n.saveDayMeta(days: {
        '2026-06-13': {'note': 'account A'}
      });
      await Future<void>.delayed(Duration.zero);
      // A signs out and B signs in while the PATCH is in flight. B's own
      // trip is also called "Trip": the same path.
      api.clearToken();
      api.setToken(fakeJwt(sub: 2));
      patchGate!.complete();
      await save;

      expect(dayMetaWrites.where((w) => w.method == 'PUT'), isEmpty,
          reason: "A's whole map must not replace B's days under B's token");
    });
  });
}
