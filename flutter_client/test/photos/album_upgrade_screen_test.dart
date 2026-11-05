import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/photos/album_upgrade_notifier.dart';
import 'package:traxjourney_client/src/photos/album_upgrade_screen.dart';
import 'package:traxjourney_client/src/photos/album_upgrade_service.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _mb = 1024 * 1024;

Map<String, dynamic> _summary({
  Map<String, int> counts = const {},
  int high = 0,
  int low = 0,
  int selected = 0,
  int delta = 0,
  int used = 0,
  int? limit,
}) =>
    {
      'counts': {
        for (final s in const [
          'proposed', 'no_match', 'already_original', 'rejected', 'applying',
          'applied', 'failed', 'stale'
        ])
          s: counts[s] ?? 0,
      },
      'confidence': {'high': high, 'low': low},
      'selected': selected,
      'selected_bytes_delta': delta,
      'storage': {'used': used, 'limit': limit},
    };

Map<String, dynamic> _scan({
  int id = 7,
  String status = 'ready',
  String? stage,
  String apply = 'idle',
  String? reason,
  String? error,
  String album = 'alb1',
  Map<String, dynamic>? summary,
  int? unreadable,
}) =>
    {
      if (unreadable != null) 'unreadable_photos': unreadable,
      'id': id,
      'album_id': album,
      'album_name': 'Japan',
      'include_memories': true,
      'include_journal': true,
      'status': status,
      'stage': stage,
      'progress_done': 3,
      'progress_total': 10,
      'error_message': error,
      'apply_status': apply,
      'apply_stopped_reason': reason,
      'created_at': 1.0,
      'completed_at': null,
      if (summary != null) 'summary': summary,
    };

Map<String, dynamic> _row(
  int id, {
  String state = 'proposed',
  String? confidence = 'high',
  bool selected = true,
  String? asset,
  String date = '2026-07-10',
  String kind = 'memory',
  int artifact = 1,
  String? err,
}) =>
    {
      'id': id,
      'artifact_kind': kind,
      'artifact_id': artifact,
      'photo_uuid': 'p$id',
      'current_uuid': 'p$id',
      'position': id,
      'date': date,
      'state': state,
      'confidence': confidence,
      'selected': selected,
      'asset_id': state == 'no_match' ? null : (asset ?? 'a$id'),
      'score': 3,
      'alternatives': [],
      'asset_bytes': 6 * _mb,
      'asset_width': 4000,
      'asset_height': 3000,
      'current_bytes': 100 * 1024,
      'current_width': 800,
      'current_height': 600,
      'error_code': err,
      'applied_uuid': null,
    };

/// A scripted server behind the `api` global.
class _Server {
  bool connected = true;
  Map<String, dynamic>? scan;
  Map<String, dynamic>? newer;
  int? memories = 5;
  int journal = 2;
  List<Map<String, dynamic>> rows = [];
  Map<String, dynamic> summaryAfterAction = _summary();
  int latestCalls = 0;
  int? scanPostStatus;
  final albums = <Map<String, dynamic>>[
    {'id': 'alb1', 'name': 'Japan', 'asset_count': 40, 'start_date': '2026-07-01', 'end_date': '2026-07-20', 'thumb_url': null, 'shared': false},
  ];
  final requests = <http.Request>[];

  Iterable<http.Request> where(String method, Pattern path) =>
      requests.where((r) => r.method == method && r.url.path.contains(path));

  Map<String, dynamic> bodyOf(http.Request r) => jsonDecode(r.body) as Map<String, dynamic>;

  http.Response _ok(Object body, [int status = 200]) => http.Response(
      jsonEncode(body), status, headers: {'content-type': 'application/json'});

  Future<http.Response> handle(http.Request r) async {
    requests.add(r);
    final path = r.url.path;
    if (path == '/api/immich/status') return _ok({'connected': connected});
    if (path == '/api/immich/albums') return _ok({'albums': albums});
    if (path.endsWith('/photo-upgrade/scans/latest')) {
      latestCalls++;
      return _ok({
        'scan': scan,
        'newer': newer,
        'photo_counts': {'memories': memories, 'journal': journal},
      });
    }
    if (path.endsWith('/photo-upgrade/scans') && r.method == 'POST') {
      if (scanPostStatus == 503) {
        return _ok({'detail': 'No worker is processing photo jobs'}, 503);
      }
      return _ok(_scan(id: 9, status: 'pending'), 202);
    }
    if (path.endsWith('/suggestions') && r.method == 'GET') {
      final filter = r.url.queryParameters['filter'] ?? '';
      final shown = rows.where((row) {
        return switch (filter) {
          '' => true,
          'confident' => row['state'] == 'proposed' && row['confidence'] == 'high',
          'to_check' => row['state'] == 'proposed' && row['confidence'] != 'high',
          _ => row['state'] == filter,
        };
      }).toList();
      return _ok({'items': shown, 'next_cursor': null});
    }
    if (r.method == 'PATCH') {
      final id = int.parse(path.split('/').last);
      final body = bodyOf(r);
      final row = rows.firstWhere((x) => x['id'] == id);
      if (body['asset_id'] != null) {
        row['asset_id'] = body['asset_id'];
        row['state'] = 'proposed';
        row['selected'] = true;
      }
      if (body['selected'] != null) {
        row['selected'] = body['selected'];
        // Like the server: ticking a failed row puts it back to proposed.
        if (body['selected'] == true && row['state'] == 'failed') {
          row['state'] = 'proposed';
          row['error_code'] = null;
        }
      }
      if (body['rejected'] == true) row['state'] = 'rejected';
      return _ok({'suggestion': row, 'summary': summaryAfterAction});
    }
    if (path.endsWith('/select')) return _ok(summaryAfterAction);
    if (path.endsWith('/candidates')) {
      return _ok({
        'candidates': [
          {'asset_id': 'b1', 'local_date': '2026-07-10T09:00:00', 'score': 2, 'mime': 'image/jpeg', 'bytes': 5 * _mb, 'width': 4000, 'height': 3000},
          {'asset_id': 'b2', 'local_date': '2026-07-10T10:00:00', 'score': 30, 'mime': 'image/jpeg', 'bytes': 4 * _mb, 'width': 4000, 'height': 3000},
        ]
      });
    }
    if (path.endsWith('/apply')) {
      return _ok(_scan(apply: 'pending'), 202);
    }
    return _ok({'detail': 'unexpected $path'}, 404);
  }
}

class _FakeProject extends ProjectNotifier {
  _FakeProject() : super(ProjectService());
  int loadCalls = 0;

  @override
  Future<void> load(ProjectRef ref) async {
    loadCalls++;
  }
}

late ApiClient _savedApi;
late _Server _server;
late _FakeProject _project;
late AlbumUpgradeNotifier _notifier;

Future<void> _pumpApp(WidgetTester tester, {Duration pollInterval = const Duration(seconds: 2)}) async {
  tester.view.physicalSize = const Size(500, 1600);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final router = GoRouter(routes: [
    GoRoute(path: '/', builder: (_, __) => const AlbumUpgradeScreen(projectName: 'Trip')),
    GoRoute(
        path: '/settings',
        builder: (_, __) => Scaffold(
                body: Column(children: [
              const Text('settings page'),
              Builder(
                  builder: (ctx) =>
                      TextButton(onPressed: () => ctx.pop(), child: const Text('back from settings'))),
            ]))),
    GoRoute(path: '/settings/plan', builder: (_, __) => const Scaffold(body: Text('plan page'))),
  ]);
  await tester.pumpWidget(MultiProvider(
    providers: [
      ChangeNotifierProvider<ProjectNotifier>.value(value: _project),
      ChangeNotifierProvider<AlbumUpgradeNotifier>(
        create: (_) => _notifier = AlbumUpgradeNotifier(pollInterval: pollInterval),
      ),
    ],
    child: MaterialApp.router(routerConfig: router),
  ));
  await _settle(tester);
}

/// Bounded settle: progress indicators spin forever, so `pumpAndSettle` hangs.
Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 10; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

Finder _inRow(int id, Finder f) => find.descendant(of: find.byKey(ValueKey('row-$id')), matching: f);

void main() {
  setUp(() {
    _savedApi = api;
    _server = _Server();
    _project = _FakeProject();
    api = ApiClient(httpClient: MockClient(_server.handle))..setToken('jwt');
  });
  tearDown(() => api = _savedApi);

  group('not connected and no worker', () {
    testWidgets('a card explains Immich and leads to Settings', (tester) async {
      _server.connected = false;
      await _pumpApp(tester);

      expect(find.text('Immich is not connected'), findsOneWidget);
      expect(find.textContaining('photo library you host yourself'), findsOneWidget);
      expect(_server.where('GET', 'photo-upgrade'), isEmpty);

      await tester.tap(find.text('Open Settings'));
      await _settle(tester);
      expect(find.text('settings page'), findsOneWidget);
    });

    testWidgets('coming back from Settings re-reads the connection and drops the card',
        (tester) async {
      _server.connected = false;
      await _pumpApp(tester);
      expect(find.text('Immich is not connected'), findsOneWidget);

      await tester.tap(find.text('Open Settings'));
      await _settle(tester);
      expect(find.text('settings page'), findsOneWidget);

      _server.connected = true; // the user connected Immich meanwhile
      await tester.tap(find.text('back from settings'));
      await _settle(tester);

      expect(_server.where('GET', '/api/immich/status'), hasLength(2));
      expect(find.text('Immich is not connected'), findsNothing);
      expect(find.byKey(const ValueKey('scan-button')), findsOneWidget);
    });

    testWidgets('a 503 on Scan says upgrades are not available right now', (tester) async {
      _server.scanPostStatus = 503;
      await _pumpApp(tester);
      expect(find.text('Nothing changes until you approve it.'), findsOneWidget);

      await tester.tap(find.text('Japan'));
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('scan-button')));
      await _settle(tester);

      expect(find.text('Photo upgrades are not available on this server right now.'),
          findsOneWidget);
    });
  });

  group('setup', () {
    testWidgets('a companion sees no Memories checkbox, the owner does', (tester) async {
      _server.memories = null;
      await _pumpApp(tester);
      expect(find.byKey(const ValueKey('include-memories')), findsNothing);
      expect(find.byKey(const ValueKey('include-journal')), findsOneWidget);
      expect(find.text('My journal entries (2 photos)'), findsOneWidget);
    });

    testWidgets('an owner sees both boxes with their counts, ticked', (tester) async {
      await _pumpApp(tester);
      expect(find.text('Memories (5 photos)'), findsOneWidget);
      expect(tester.widget<CheckboxListTile>(find.byKey(const ValueKey('include-memories'))).value,
          isTrue);
    });

    test('albums overlapping the trip come first, and the last-used one is preselected',
        () async {
      _server
        ..albums.clear()
        ..albums.addAll([
          {'id': 'old', 'name': 'Old', 'start_date': '2020-01-01T00:00:00', 'end_date': '2020-01-05', 'shared': false},
          {'id': 'trip', 'name': 'Trip', 'start_date': '2026-07-05T08:00:00', 'end_date': '2026-07-09', 'shared': false},
        ])
        ..scan = _scan(album: 'old', summary: _summary());
      final n = AlbumUpgradeNotifier(
          service: AlbumUpgradeService(client: api));
      await n.load(const ProjectRef(name: 'Trip'), tripStart: '2026-07-01', tripEnd: '2026-07-31');
      n.newScan();
      await Future<void>.delayed(Duration.zero);
      await Future<void>.delayed(Duration.zero);

      expect(n.albums.map((a) => a.id), ['trip', 'old']);
      expect(n.selectedAlbumId, 'old');
      n.dispose();
    });
  });

  group('review', () {
    void review({List<Map<String, dynamic>>? rows, Map<String, dynamic>? summary, Map<String, dynamic>? scan}) {
      _server.rows = rows ??
          [
            _row(1),
            _row(2, confidence: 'low', selected: false, date: '2026-07-11'),
          ];
      _server.scan = scan ??
          _scan(summary: summary ?? _summary(counts: {'proposed': 2}, high: 1, low: 1, selected: 1, delta: 5 * _mb, used: 10 * _mb, limit: 100 * _mb));
    }

    testWidgets('confident rows are ticked, others are not, grouped by day', (tester) async {
      review();
      await _pumpApp(tester);

      expect(tester.widget<Checkbox>(find.byKey(const ValueKey('row-check-1'))).value, isTrue);
      expect(tester.widget<Checkbox>(find.byKey(const ValueKey('row-check-2'))).value, isFalse);
      expect(find.text('2026-07-10'), findsOneWidget);
      expect(find.text('2026-07-11'), findsOneWidget);
      expect(find.text('Confident match'), findsOneWidget);
      expect(find.text('Please check this one'), findsOneWidget);
      expect(find.text('Original: 6.0 MB · 4000×3000'), findsNWidgets(2));
    });

    testWidgets('the summary chips filter the list through the server', (tester) async {
      review();
      await _pumpApp(tester);

      await tester.tap(find.byKey(const ValueKey('chip-confident')));
      await _settle(tester);
      expect(_server.requests.last.url.queryParameters['filter'], 'confident');
      expect(find.byKey(const ValueKey('row-1')), findsOneWidget);
      expect(find.byKey(const ValueKey('row-2')), findsNothing);

      await tester.tap(find.byKey(const ValueKey('chip-confident')));
      await _settle(tester);
      expect(find.byKey(const ValueKey('row-2')), findsOneWidget);
    });

    testWidgets('Choose another lists the day and sends the pick in a PATCH', (tester) async {
      review();
      await _pumpApp(tester);

      await tester.tap(_inRow(1, find.text('Choose another')));
      await _settle(tester);
      expect(_server.where('GET', '/candidates').single.url.queryParameters['suggestion_id'], '1');

      await tester.tap(find.byKey(const ValueKey('candidate-b2')));
      await _settle(tester);

      final patch = _server.where('PATCH', '/suggestions/1').single;
      expect(_server.bodyOf(patch), {'asset_id': 'b2'});
    });

    testWidgets('a tick sends selected, Not this one sends rejected', (tester) async {
      review();
      await _pumpApp(tester);

      await tester.tap(find.byKey(const ValueKey('row-check-2')));
      await _settle(tester);
      expect(_server.bodyOf(_server.where('PATCH', '/suggestions/2').single), {'selected': true});

      await tester.tap(_inRow(1, find.text('Not this one')));
      await _settle(tester);
      expect(_server.bodyOf(_server.where('PATCH', '/suggestions/1').single), {'rejected': true});
    });

    testWidgets('No match rows offer Choose from album', (tester) async {
      review(rows: [_row(1, state: 'no_match', confidence: null, selected: false)]);
      await _pumpApp(tester);
      expect(_inRow(1, find.text('Choose from album')), findsOneWidget);
    });

    testWidgets('the bar shows the count, the size and the free space', (tester) async {
      review(summary: _summary(counts: {'proposed': 3}, high: 3, selected: 3, delta: 5 * _mb, used: 40 * _mb, limit: 100 * _mb));
      await _pumpApp(tester);

      expect(find.text('Replace 3 photos · +5.0 MB of 60 MB free'), findsOneWidget);
      expect(tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Replace')).onPressed,
          isNotNull);
      expect(find.text('Upgrade your plan'), findsNothing);
    });

    testWidgets('over storage the bar blocks Replace and links to the plan', (tester) async {
      review(summary: _summary(counts: {'proposed': 3}, high: 3, selected: 3, delta: 80 * _mb, used: 40 * _mb, limit: 100 * _mb));
      await _pumpApp(tester);

      expect(tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Replace')).onPressed,
          isNull);
      expect(find.text('Not enough storage for these photos.'), findsOneWidget);
      await tester.tap(find.text('Upgrade your plan'));
      await _settle(tester);
      expect(find.text('plan page'), findsOneWidget);
    });

    testWidgets('Select all confident and Clear call select; Replace calls apply', (tester) async {
      review();
      _server.summaryAfterAction = _summary(
          counts: {'proposed': 2}, high: 1, low: 1, selected: 1, delta: _mb, limit: 100 * _mb);
      await _pumpApp(tester);

      await tester.tap(find.text('Select all confident'));
      await _settle(tester);
      expect(_server.bodyOf(_server.where('POST', '/select').last), {'which': 'confident'});
      await tester.tap(find.text('Clear'));
      await _settle(tester);
      expect(_server.bodyOf(_server.where('POST', '/select').last), {'which': 'none'});

      await tester.tap(find.widgetWithText(FilledButton, 'Replace'));
      await _settle(tester);
      expect(_server.where('POST', '/apply'), hasLength(1));
      expect(find.textContaining('Replacing photos…'), findsOneWidget);
    });

    testWidgets('a failed newer scan shows the earlier review under a banner', (tester) async {
      review();
      _server.newer = _scan(id: 9, status: 'failed', error: 'Immich went away.', album: 'alb9');
      await _pumpApp(tester);

      expect(find.byKey(const ValueKey('row-1')), findsOneWidget);
      expect(find.textContaining('The latest check of your album failed. Immich went away.'),
          findsOneWidget);

      await tester.tap(find.text('Try again'));
      await _settle(tester);
      final post = _server.where('POST', '/photo-upgrade/scans').single;
      expect(_server.bodyOf(post)['album_id'], 'alb9');
    });
  });

  group('apply states', () {
    testWidgets('applied rows keep Choose another', (tester) async {
      _server.rows = [_row(1, state: 'applied', selected: false)];
      _server.scan = _scan(apply: 'done', summary: _summary(counts: {'applied': 1}));
      await _pumpApp(tester);

      expect(find.text('Replaced with the original'), findsOneWidget);
      expect(find.text('1 photo replaced with the originals.'), findsOneWidget);
      expect(_inRow(1, find.text('Choose another')), findsOneWidget);
    });

    testWidgets('a failed row gives its reason in words', (tester) async {
      _server.rows = [
        _row(1, state: 'failed', selected: false, err: 'too_large'),
        _row(2, state: 'failed', selected: false, err: 'immich_permission'),
      ];
      _server.scan = _scan(summary: _summary(counts: {'failed': 2}));
      await _pumpApp(tester);

      expect(find.textContaining('larger than 25 MB'), findsOneWidget);
      expect(find.textContaining('asset.download'), findsOneWidget);
      expect(find.text('2 photos could not be replaced.'), findsOneWidget);
    });

    testWidgets('a failed row is unticked even though the server keeps selected, and a tap ticks it',
        (tester) async {
      _server.rows = [_row(1, state: 'failed', selected: true, err: 'immich_unreachable')];
      _server.scan = _scan(summary: _summary(counts: {'failed': 1}));
      await _pumpApp(tester);

      expect(tester.widget<Checkbox>(find.byKey(const ValueKey('row-check-1'))).value, isFalse);

      await tester.tap(find.byKey(const ValueKey('row-check-1')));
      await _settle(tester);
      expect(_server.bodyOf(_server.where('PATCH', '/suggestions/1').single), {'selected': true});
      expect(tester.widget<Checkbox>(find.byKey(const ValueKey('row-check-1'))).value, isTrue);
    });

    testWidgets('compare asks Immich for the preview and never the original', (tester) async {
      _server.rows = [_row(1)];
      _server.scan = _scan(summary: _summary(counts: {'proposed': 1}, high: 1));
      await _pumpApp(tester);

      await tester.tap(find.byKey(const ValueKey('row-compare-1')));
      await _settle(tester);

      final urls = tester
          .widgetList<Image>(find.byType(Image))
          .map((i) => i.image)
          .whereType<NetworkImage>()
          .map((i) => i.url)
          .toList();
      expect(urls.where((u) => u.contains('/api/immich/assets/a1/thumbnail?size=preview')),
          hasLength(1));
      expect(urls.where((u) => u.contains('/original')), isEmpty);
      expect(find.text('Immich preview'), findsOneWidget);
    });

    testWidgets('the unreadable-photos notice shows at N>0 with plural handling',
        (tester) async {
      _server.rows = [_row(1)];
      _server.scan = _scan(unreadable: 3, summary: _summary(counts: {'proposed': 1}, high: 1));
      await _pumpApp(tester);
      expect(
          find.text('3 photos in the album could not be read from Immich, so they could not '
              'be matched. Scan again to retry.'),
          findsOneWidget);
    });

    testWidgets('one unreadable photo reads in the singular', (tester) async {
      _server.rows = [_row(1)];
      _server.scan = _scan(unreadable: 1, summary: _summary(counts: {'proposed': 1}, high: 1));
      await _pumpApp(tester);
      expect(find.textContaining('1 photo in the album could not be read'), findsOneWidget);
    });

    testWidgets('no unreadable notice at 0 or when the field is missing', (tester) async {
      _server.rows = [_row(1)];
      _server.scan = _scan(unreadable: 0, summary: _summary(counts: {'proposed': 1}, high: 1));
      await _pumpApp(tester);
      expect(find.byKey(const ValueKey('unreadable-notice')), findsNothing);

      _server.scan = _scan(summary: _summary(counts: {'proposed': 1}, high: 1)); // omitted
      await _notifier.load(const ProjectRef(name: 'Trip'));
      await _settle(tester);
      expect(find.byKey(const ValueKey('row-1')), findsOneWidget);
      expect(find.byKey(const ValueKey('unreadable-notice')), findsNothing);
    });

    testWidgets('Retry failed calls select with which failed', (tester) async {
      _server.rows = [_row(1, state: 'failed', selected: false, err: 'immich_permission')];
      _server.scan = _scan(summary: _summary(counts: {'failed': 1}));
      await _pumpApp(tester);

      await tester.tap(find.text('Retry failed'));
      await _settle(tester);
      expect(_server.bodyOf(_server.where('POST', '/select').single), {'which': 'failed'});
    });

    testWidgets('Retry failed is not offered when no failure can pass', (tester) async {
      _server.rows = [_row(1, state: 'failed', selected: false, err: 'too_large')];
      _server.scan = _scan(summary: _summary(counts: {'failed': 1}));
      await _pumpApp(tester);
      expect(find.text('Retry failed'), findsNothing);
    });

    testWidgets('a stop for storage explains itself and Resume applies again', (tester) async {
      _server.rows = [_row(1)];
      _server.scan = _scan(
          apply: 'stopped',
          reason: 'storage_full',
          summary: _summary(counts: {'proposed': 1}, high: 1, selected: 1, delta: _mb));
      await _pumpApp(tester);

      expect(find.textContaining('stopped because your storage is full'), findsOneWidget);
      expect(find.text('Upgrade your plan'), findsOneWidget);
      await tester.tap(find.text('Resume'));
      await _settle(tester);
      expect(_server.where('POST', '/apply'), hasLength(1));
    });

    testWidgets('an interrupted apply offers Resume', (tester) async {
      _server.rows = [_row(1)];
      _server.scan = _scan(
          apply: 'stopped',
          reason: 'interrupted',
          summary: _summary(counts: {'proposed': 1}, high: 1, selected: 1, delta: _mb));
      await _pumpApp(tester);

      expect(find.textContaining('Replacing was interrupted'), findsOneWidget);
      await tester.tap(find.text('Resume'));
      await _settle(tester);
      expect(_server.where('POST', '/apply'), hasLength(1));
    });

    testWidgets('while applying, rows flip as polled and the project reloads once at the end',
        (tester) async {
      _server.rows = [_row(1), _row(2)];
      _server.scan = _scan(
          apply: 'running',
          summary: _summary(counts: {'proposed': 2}, high: 2, selected: 2, delta: _mb));
      await _pumpApp(tester);

      expect(find.textContaining('Replacing photos…'), findsOneWidget);
      expect(find.widgetWithText(FilledButton, 'Replace'), findsNothing); // bar hidden
      expect(tester.widget<Checkbox>(find.byKey(const ValueKey('row-check-1'))).onChanged, isNull);

      _server.rows = [_row(1, state: 'applied', selected: false), _row(2)];
      await tester.pump(const Duration(seconds: 2));
      await _settle(tester);
      expect(find.text('Replaced with the original'), findsOneWidget);
      expect(_project.loadCalls, 0);

      _server.rows = [_row(1, state: 'applied', selected: false), _row(2, state: 'applied', selected: false)];
      _server.scan = _scan(apply: 'done', summary: _summary(counts: {'applied': 2}));
      await tester.pump(const Duration(seconds: 2));
      await _settle(tester);
      expect(_project.loadCalls, 1);
      expect(find.text('2 photos replaced with the originals.'), findsOneWidget);

      final calls = _server.latestCalls;
      await tester.pump(const Duration(seconds: 10));
      expect(_server.latestCalls, calls); // polling stopped with the apply
      expect(_project.loadCalls, 1);
    });
  });

  group('scanning', () {
    testWidgets('shows the stage and progress, polls, and stops when the page is left',
        (tester) async {
      _server.newer = _scan(id: 8, status: 'scanning', stage: 'fingerprinting');
      await _pumpApp(tester);

      expect(find.text('Comparing your photos with the album…'), findsOneWidget);
      expect(find.text('3 / 10'), findsOneWidget);
      expect(_server.latestCalls, 1);

      await tester.pump(const Duration(seconds: 2));
      await _settle(tester);
      expect(_server.latestCalls, 2);
      await tester.pump(const Duration(seconds: 2));
      await _settle(tester);
      expect(_server.latestCalls, 3);

      // Leave the page: the provider disposes the notifier and its timer.
      await tester.pumpWidget(const SizedBox());
      await tester.pump(const Duration(seconds: 20));
      expect(_server.latestCalls, 3);
    });

    testWidgets('a finished scan turns into the review', (tester) async {
      _server.newer = _scan(id: 8, status: 'scanning', stage: 'matching');
      await _pumpApp(tester);

      _server.newer = null;
      _server.scan = _scan(id: 8, summary: _summary(counts: {'proposed': 1}, high: 1, selected: 1, delta: _mb));
      _server.rows = [_row(1)];
      await tester.pump(const Duration(seconds: 2));
      await _settle(tester);

      expect(find.byKey(const ValueKey('row-1')), findsOneWidget);
      expect(_notifier.isPolling, isFalse);
    });
  });
}
