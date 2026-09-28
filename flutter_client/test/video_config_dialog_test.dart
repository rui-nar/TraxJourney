// Widget tests for the trip-video dialog (docs/TRIP_VIDEO_PLAN.md, U7): the
// consent dialog on an encrypted trip (geometry sent only after "Send and
// continue", nothing on "Decline"), the upgrade message on a 402 and the
// unavailable state; tracks /meta deferred fetched with progress; and no way
// to close the dialog while a job is being created (U7a).
//
// The dialog shows a CircularProgressIndicator while busy, so pumpAndSettle
// would never return then: fixed frames are pumped instead.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/video_config_dialog.dart';
import 'package:traxjourney_client/src/projects/video_consent_dialog.dart';
import 'package:traxjourney_client/src/projects/video_job_notifier.dart';

http.Response _json(int status, Object body) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _plan({bool available = true, List<int> res = const [720]}) => {
      'available': available,
      'length_s': 60,
      'legs': 4,
      'clips': [],
      'clip_counts': {'30': 2, '60': 4, '90': 4},
      'skipped': [],
      'consent_required': [],
      'quota': {'limit': 1, 'used': 0, 'remaining': 1},
      'resolutions': res,
    };

final _consent409 = {
  'detail': {
    'code': 'consent_required',
    'message': 'This trip is encrypted.',
    'consent_required': [7],
  }
};

const _track = '_p~iF~ps|U_ulLnnqC_mqNvxq`@';

List<Map<String, dynamic>> _activities() => [
      {
        'id': 7,
        'map': {'summary_polyline': _track},
      },
    ];

Future<void> _frames(WidgetTester tester) async {
  for (var i = 0; i < 8; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

void main() {
  late List<http.Request> sent;
  late int? startedJob;

  Future<void> open(WidgetTester tester,
      Future<http.Response> Function(http.Request) handler,
      {List<Map<String, dynamic>> Function() activities = _activities,
      TrackFetcher? fetchTrack}) async {
    sent = [];
    startedJob = null;
    final client = ApiClient(httpClient: MockClient((req) async {
      sent.add(req);
      return handler(req);
    }))
      ..setToken('jwt');
    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => Scaffold(
          body: TextButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => VideoConfigDialog(
                projectRef: const ProjectRef(name: 'Trip'),
                activities: activities,
                onStarted: (id) => startedJob = id,
                client: client,
                reveal: (v) async => v,
                fetchTrack: fetchTrack,
              ),
            ),
            child: const Text('open'),
          ),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await _frames(tester);
  }

  bool hasGeometry(http.Request r) =>
      (jsonDecode(r.body) as Map).containsKey('decrypted_geometry');

  testWidgets('shows lengths with clip counts, resolutions and the quota',
      (tester) async {
    await open(tester, (_) async => _json(200, _plan()));
    expect(find.text('30 s'), findsOneWidget);
    expect(find.text('60 s'), findsOneWidget);
    expect(find.text('90 s'), findsOneWidget);
    expect(find.text('2 clips'), findsOneWidget);
    expect(find.text('720p'), findsOneWidget);
    expect(find.text('1080p'), findsNothing);
    expect(find.text('1080p is available on paid plans.'), findsOneWidget);
    expect(find.text('1 of 1 video left this month.'), findsOneWidget);
  });

  testWidgets('encrypted trip: decline sends nothing and closes', (tester) async {
    await open(tester, (_) async => _json(409, _consent409));
    expect(find.byType(VideoConsentDialog), findsOneWidget);
    expect(find.textContaining('deleted as soon as it finishes'), findsOneWidget);

    await tester.tap(find.text('Decline'));
    await _frames(tester);

    expect(find.byType(VideoConsentDialog), findsNothing);
    expect(find.byType(VideoConfigDialog), findsNothing);
    expect(sent, hasLength(1));
    expect(hasGeometry(sent.single), isFalse);
  });

  testWidgets('encrypted trip: geometry is sent only after accepting, then '
      'the video is created with it', (tester) async {
    await open(tester, (req) async {
      if (!hasGeometry(req)) return _json(409, _consent409);
      return req.url.path.endsWith('/plan')
          ? _json(200, _plan())
          : _json(201, {'job_id': 11});
    });
    expect(find.byType(VideoConsentDialog), findsOneWidget);
    expect(sent.where(hasGeometry), isEmpty);

    await tester.tap(find.text('Send and continue'));
    await _frames(tester);

    expect(sent, hasLength(2));
    final geometry = (jsonDecode(sent.last.body) as Map)['decrypted_geometry'];
    expect(geometry, {'7': _track});
    expect(find.text('Includes 1 decrypted track, deleted after the render.'),
        findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Create video'));
    await _frames(tester);

    expect(sent.last.url.path, '/api/projects/Trip/video');
    expect((jsonDecode(sent.last.body) as Map)['decrypted_geometry'],
        {'7': _track});
    expect(startedJob, 11);
    expect(find.byType(VideoConfigDialog), findsNothing);
  });

  testWidgets('402 shows the upgrade message', (tester) async {
    await open(tester, (req) async {
      if (req.url.path.endsWith('/plan')) return _json(200, _plan());
      return _json(402, {
        'detail': 'Your plan includes 1 video per month. Upgrade to make more.',
        'code': 'quota_exceeded',
        'resource': 'videos',
        'plan': 'free',
        'limit': 1,
        'used': 1,
        'needed': 2,
      });
    });
    await tester.tap(find.widgetWithText(FilledButton, 'Create video'));
    await _frames(tester);

    expect(find.text('Your plan includes 1 video per month. Upgrade to make more.'),
        findsOneWidget);
    expect(find.text('See plans'), findsOneWidget);
    expect(startedJob, isNull);
    final create = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Create video'));
    expect(create.onPressed, isNull);
  });

  testWidgets('unavailable: says so and cannot create', (tester) async {
    await open(tester, (_) async => _json(200, _plan(available: false)));
    expect(find.textContaining("Video rendering isn't available"), findsOneWidget);
    final create = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Create video'));
    expect(create.onPressed, isNull);
  });

  group('U7a', () {
    // /meta deferred 7's polyline: the consent step has to fetch it.
    List<Map<String, dynamic>> deferred() => [
          {
            'id': 7,
            'map': {'summary_polyline': null},
          },
        ];

    Future<http.Response> consentOnCreate(http.Request req) async {
      if (req.url.path.endsWith('/plan')) return _json(200, _plan());
      return hasGeometry(req)
          ? _json(201, {'job_id': 12})
          : _json(409, _consent409);
    }

    bool isCreate(http.Request r) => r.url.path == '/api/projects/Trip/video';

    testWidgets('Cancel, Escape and back do nothing while the job is being '
        'created; the job is then reported', (tester) async {
      final created = Completer<http.Response>();
      await open(tester, (req) async {
        if (req.url.path.endsWith('/plan')) return _json(200, _plan());
        return created.future;
      });
      await tester.tap(find.widgetWithText(FilledButton, 'Create video'));
      await _frames(tester);

      final cancel =
          tester.widget<TextButton>(find.widgetWithText(TextButton, 'Cancel'));
      expect(cancel.onPressed, isNull);
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await _frames(tester);
      await tester.binding.handlePopRoute();
      await _frames(tester);
      expect(find.byType(VideoConfigDialog), findsOneWidget);

      created.complete(_json(201, {'job_id': 13}));
      await _frames(tester);
      expect(startedJob, 13);
      expect(find.byType(VideoConfigDialog), findsNothing);
    });

    testWidgets('a deferred track is fetched with progress shown and sent '
        'decrypted', (tester) async {
      final gate = Completer<Map<String, dynamic>?>();
      await open(tester, consentOnCreate,
          activities: deferred, fetchTrack: (_) => gate.future);
      await tester.tap(find.widgetWithText(FilledButton, 'Create video'));
      await _frames(tester);
      await tester.tap(find.text('Send and continue'));
      await _frames(tester);
      expect(find.text('Decrypting tracks on this device: 0 of 1'),
          findsOneWidget);

      gate.complete({
        'id': 7,
        'map': {'summary_polyline': _track},
      });
      await _frames(tester);
      expect((jsonDecode(sent.last.body) as Map)['decrypted_geometry'],
          {'7': _track});
      expect(startedJob, 12);
    });

    testWidgets('Cancel while tracks are being fetched sends no job',
        (tester) async {
      final gate = Completer<Map<String, dynamic>?>();
      await open(tester, consentOnCreate,
          activities: deferred, fetchTrack: (_) => gate.future);
      await tester.tap(find.widgetWithText(FilledButton, 'Create video'));
      await _frames(tester);
      await tester.tap(find.text('Send and continue'));
      await _frames(tester);
      expect(sent.where(isCreate), hasLength(1));

      await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
      await _frames(tester);
      expect(find.byType(VideoConfigDialog), findsNothing);

      gate.complete({
        'id': 7,
        'map': {'summary_polyline': _track},
      });
      await _frames(tester);
      expect(sent.where(isCreate), hasLength(1),
          reason: 'only the create the 409 answered');
      expect(startedJob, isNull);
    });

    testWidgets('a failed track fetch sends nothing and says so',
        (tester) async {
      await open(tester, consentOnCreate,
          activities: deferred,
          fetchTrack: (_) async => throw Exception('offline'));
      await tester.tap(find.widgetWithText(FilledButton, 'Create video'));
      await _frames(tester);
      await tester.tap(find.text('Send and continue'));
      await _frames(tester);

      expect(find.textContaining("Couldn't load this trip's encrypted tracks"),
          findsOneWidget);
      expect(sent.where(hasGeometry), isEmpty);
      expect(startedJob, isNull);
    });
  });
}
