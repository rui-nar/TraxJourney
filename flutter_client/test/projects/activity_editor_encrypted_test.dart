/// Editing, splitting and resetting an encrypted activity's track on the
/// device (E2EE remnants decision 7): the server never receives the track.
///
/// Save and split are measured with the `track_metrics/` port against the
/// track the editor opened, encrypted, and sent to the encrypted routes; every
/// geometry value in those bodies is an envelope and no coordinate of the
/// track appears in them. Plaintext activities keep today's requests; reset
/// sends the editor's lock_version and explains the server's 409s.
///
/// The app's real `encryption` singleton is used; it talks to the server
/// through the `api` client current when it is first used, so every test
/// shares one client whose answers [_answer] decides.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart' show EncryptedField;
import 'package:traxjourney_client/src/crypto/encryption.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/activity_editor_page.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/track_edit_model.dart';
import 'package:traxjourney_client/src/projects/track_editor_controller.dart';
import 'package:traxjourney_client/src/track_metrics/align.dart';
import 'package:traxjourney_client/src/track_metrics/elevation_profile.dart';
import 'package:traxjourney_client/src/track_metrics/polyline_encoder.dart';
import 'package:traxjourney_client/src/track_metrics/track_metrics.dart';

import '../track_metrics/vectors.dart';

const _ref = ProjectRef(name: 'Trip');
const _id = 42;
const _lockVersion = 7;
const _movingTime = 3600;
const _elapsedTime = 4000;
const _storedGain = 250.0;

/// The vector track the Python maths pinned: a 12-point polyline with a
/// profile sampled on its own points.
final Map<String, dynamic> _vector = vectorCases('align_points')
    .firstWhere((c) => c['name'] == 'profile_from_the_same_points_round_trips');
String get _polyline => _vector['input']['summary_polyline'] as String;
ElevationProfile get _profile {
  final ep = _vector['input']['elevation_profile'] as Map<String, dynamic>;
  return ElevationProfile(doubles(ep['distances_km']), doubles(ep['elevations_m']));
}

/// Every request the client sent, and the answer each route gives.
final List<http.Request> _requests = [];
http.Response Function(http.Request req) _answer = _ok;

http.Response _ok(http.Request req) {
  final path = req.url.path;
  if (path.endsWith('/track/encrypted')) {
    return http.Response(jsonEncode({'id': _id, 'lock_version': _lockVersion + 1}), 200);
  }
  if (path.endsWith('/split/encrypted')) {
    return http.Response(
        jsonEncode({'id': _id, 'tail_id': -3, 'lock_version': _lockVersion + 1}), 200);
  }
  return http.Response('{}', 200);
}

List<http.Request> _sentTo(String suffix) =>
    [for (final r in _requests) if (r.url.path.endsWith(suffix)) r];

Map<String, dynamic> _body(http.Request r) => jsonDecode(r.body) as Map<String, dynamic>;

Future<String> _decrypt(Object? envelope) => encryption.decryptText(envelope! as String);

/// The `GET …/track` response of the encrypted vector activity, as stored.
Future<Map<String, dynamic>> _storedTrack({bool edited = false}) async => {
      'id': _id,
      'name': await encryption.encryptText('Col du Lautaret'),
      'is_edited': edited,
      'moving_time': _movingTime,
      'elapsed_time': _elapsedTime,
      'total_elevation_gain': _storedGain,
      'map': {'summary_polyline': await encryption.encryptText(_polyline)},
      'elevation_profile': null,
      'elevation_profile_enc': await encryption.encryptText(jsonEncode(
          {'distances_km': _profile.distancesKm, 'elevations_m': _profile.elevationsM})),
      'start_latlng': null,
      'start_latlng_enc': await encryption.encryptText('[0.0, 0.0]'),
      'lock_version': _lockVersion,
    };

List<EditPoint> _openedPoints() => [
      for (final p in alignPoints(_polyline, _profile)) EditPoint(p.lat, p.lng, p.elev),
    ];

/// What the port computes for [points] measured against the opened track.
Map<String, Object?> _portFigures(List<EditPoint> points) {
  final before = recomputeTrackMetrics(alignPoints(_polyline, _profile));
  final m = recomputeTrackMetrics(
    [for (final p in points) TrackPoint(p.lat, p.lng, p.elev)],
    originalDistanceM: before.distance,
    originalMovingTime: _movingTime,
    originalElapsedTime: _elapsedTime,
  );
  return {
    'distance': m.distance,
    'moving_time': m.movingTime,
    'elapsed_time': m.elapsedTime,
    'average_speed': m.averageSpeed,
    'total_elevation_gain':
        apportionGain(_storedGain, before.totalElevationGain, m.totalElevationGain),
    'elev_high': m.elevHigh,
    'elev_low': m.elevLow,
  };
}

const _geometryKeys = {
  'summary_polyline',
  'elevation_profile_json',
  'start_latlng_json',
  'end_latlng_json',
};
const _figureKeys = {
  'distance',
  'moving_time',
  'elapsed_time',
  'average_speed',
  'total_elevation_gain',
  'elev_high',
  'elev_low',
};

/// The request body carries no plaintext coordinate of [points]: every
/// string in it is a well-formed envelope, every number is a figure or the
/// lock version, and neither the plaintext polyline nor any coordinate's
/// decimal form appears anywhere in the raw text.
void _expectNoPlaintextTrack(http.Request req, List<EditPoint> points) {
  final raw = req.body;
  expect(raw, isNot(contains(_polyline)));
  expect(raw, isNot(contains(encodePolyline([for (final p in points) (p.lat, p.lng)]))));
  for (final p in points) {
    for (final c in [p.lat, p.lng]) {
      expect(raw, isNot(contains(c.toString())), reason: 'coordinate $c in the body');
      expect(raw, isNot(contains(c.toStringAsFixed(4))), reason: 'coordinate $c in the body');
    }
  }
  void walk(Object? v, String key) {
    if (v is Map) {
      v.forEach((k, x) => walk(x, k as String));
    } else if (v is List) {
      fail('$key: a list in an encrypted body');
    } else if (v is String) {
      expect(EncryptedField.isWellFormed(v), isTrue, reason: '$key is not an envelope');
    } else if (v is num) {
      expect({..._figureKeys, 'lock_version'}, contains(key), reason: '$key is a number');
    }
  }

  walk(jsonDecode(raw), r'$');
}

/// Checks one encrypted piece: its figures are the port's for [points], and
/// its envelopes decrypt to the polyline, profile and endpoints of [points].
Future<void> _expectPiece(Map<String, dynamic> piece, List<EditPoint> points) async {
  expect(piece.keys.toSet(), {..._geometryKeys, ..._figureKeys});
  final figures = _portFigures(points);
  for (final key in _figureKeys) {
    expect(piece[key], figures[key], reason: key);
  }
  expect(await _decrypt(piece['summary_polyline']),
      encodePolyline([for (final p in points) (p.lat, p.lng)]));
  final ep = pointsToElevationProfile(
      [for (final p in points) TrackPoint(p.lat, p.lng, p.elev)])!;
  final profile = jsonDecode(await _decrypt(piece['elevation_profile_json'])) as Map;
  expect(profile['distances_km'], ep.distancesKm);
  expect(profile['elevations_m'], ep.elevationsM);
  expect(jsonDecode(await _decrypt(piece['start_latlng_json'])),
      [points.first.lat, points.first.lng]);
  expect(jsonDecode(await _decrypt(piece['end_latlng_json'])),
      [points.last.lat, points.last.lng]);
}

ProjectNotifier _notifier() => ProjectNotifier(ProjectService())..ref = _ref;

Future<void> _pumpEditor(WidgetTester tester, Widget page) async {
  tester.view.physicalSize = const Size(1200, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (ctx) => TextButton(
          onPressed: () =>
              Navigator.of(ctx).push(MaterialPageRoute(builder: (_) => page)),
          child: const Text('open editor'),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open editor'));
  await tester.pumpAndSettle();
}

TrackEditorController _controllerOf(WidgetTester tester) {
  final state = tester.state<State>(find.byType(ActivityEditorPage));
  return (state as dynamic).editorControllerForTest as TrackEditorController;
}

void main() {
  setUpAll(() async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          _requests.add(req);
          return _answer(req);
        }));
    FlutterSecureStorage.setMockInitialValues({});
    await encryption.enable(const RecoveryKeyChoice());
  });

  setUp(() {
    projectDataCache.resetForTest();
    _requests.clear();
    _answer = _ok;
    expect(encryption.isUnlocked, isTrue);
  });

  group('opening', () {
    test('decrypts the stored track, profile, name and figures', () async {
      final opened = await EncryptedTrackEdit.open(await _storedTrack());

      expect(opened.polyline, _polyline);
      expect(opened.profile!.distancesKm, _profile.distancesKm);
      expect(opened.profile!.elevationsM, _profile.elevationsM);
      expect(opened.name, 'Col du Lautaret');
      expect((opened.movingTime, opened.elapsedTime), (_movingTime, _elapsedTime));
      expect(opened.totalElevationGain, _storedGain);
      expect(opened.lockVersion, _lockVersion);
    });

    test("the editor starts from align_points' own points", () {
      final model = TrackEditModel.aligned(_polyline, _profile);
      final expected = vectorCases('align_points')
          .firstWhere((c) => c['name'] == 'profile_from_the_same_points_round_trips');
      expectSame([for (final p in model.points) [p.lat, p.lng, p.elev]],
          expected['expected'], r'$');
    });
  });

  group('save', () {
    test('sends only envelopes and the figures the port computes', () async {
      final opened = await EncryptedTrackEdit.open(await _storedTrack());
      final points = _openedPoints().sublist(2, 10); // trimmed at both ends

      await _notifier().saveEncryptedActivityTrack(_id, opened, points);

      final put = _sentTo('/activities/$_id/track/encrypted').single;
      expect(put.method, 'PUT');
      _expectNoPlaintextTrack(put, _openedPoints());
      final body = _body(put);
      expect(body['lock_version'], _lockVersion);
      await _expectPiece(body..remove('lock_version'), points);
      // Nothing went to the plaintext route.
      expect(_sentTo('/activities/$_id/track'), isEmpty);
    });

    test('an unchanged track keeps its times and gain exactly', () async {
      final opened = await EncryptedTrackEdit.open(await _storedTrack());

      await _notifier().saveEncryptedActivityTrack(_id, opened, _openedPoints());

      final body = _body(_sentTo('/track/encrypted').single);
      expect(body['moving_time'], _movingTime);
      expect(body['elapsed_time'], _elapsedTime);
      expect(body['total_elevation_gain'], closeTo(_storedGain, 1e-9));
    });

    test('a track with no elevations sends the profile and its bounds as null',
        () async {
      final opened = await EncryptedTrackEdit.open(await _storedTrack());
      final flat = [for (final p in _openedPoints()) EditPoint(p.lat, p.lng)];

      await _notifier().saveEncryptedActivityTrack(_id, opened, flat);

      final body = _body(_sentTo('/track/encrypted').single);
      expect(body['elevation_profile_json'], isNull);
      expect(body['elev_high'], isNull);
      expect(body['elev_low'], isNull);
      expect(EncryptedField.isWellFormed(body['summary_polyline'] as String), isTrue);
    });
  });

  group('split', () {
    test('sends two encrypted pieces whose times sum as the port computes, '
        'and an encrypted tail name', () async {
      final opened = await EncryptedTrackEdit.open(await _storedTrack());
      final points = _openedPoints();

      await _notifier().splitEncryptedActivity(_id, opened, points, 5);

      final post = _sentTo('/activities/$_id/split/encrypted').single;
      expect(post.method, 'POST');
      _expectNoPlaintextTrack(post, points);
      final body = _body(post);
      expect(body.keys.toSet(), {'head', 'tail', 'tail_name', 'lock_version'});
      expect(body['lock_version'], _lockVersion);
      final head = body['head'] as Map<String, dynamic>;
      final tail = body['tail'] as Map<String, dynamic>;
      await _expectPiece(head, points.sublist(0, 6));
      await _expectPiece(tail, points.sublist(5));
      // Both pieces are apportioned against the same opened track, so they
      // share out its times (to a second of rounding each) and its gain.
      expect((head['moving_time'] as int) + (tail['moving_time'] as int),
          inInclusiveRange(_movingTime - 1, _movingTime + 1));
      expect((head['elapsed_time'] as int) + (tail['elapsed_time'] as int),
          inInclusiveRange(_elapsedTime - 1, _elapsedTime + 1));
      expect(await _decrypt(body['tail_name']), 'Col du Lautaret (2)');
      expect(_sentTo('/activities/$_id/split'), isEmpty);
    });

    test('a cut for transport drops the boundary point from the tail', () async {
      final opened = await EncryptedTrackEdit.open(await _storedTrack());
      final points = _openedPoints();

      await _notifier()
          .splitEncryptedActivity(_id, opened, points, 5, dropBoundary: true);

      final body = _body(_sentTo('/split/encrypted').single);
      await _expectPiece(body['head'] as Map<String, dynamic>, points.sublist(0, 6));
      await _expectPiece(body['tail'] as Map<String, dynamic>, points.sublist(6));
    });
  });

  group('the editor page', () {
    testWidgets('an encrypted activity saves to the encrypted route', (tester) async {
      final stored = (await tester.runAsync(_storedTrack))!;
      final opened = (await tester.runAsync(() => EncryptedTrackEdit.open(stored)))!;
      await _pumpEditor(tester,
          ActivityEditorPage(notifier: _notifier(), activity: stored, encrypted: opened));

      expect(find.text('Edit — Col du Lautaret'), findsOneWidget);
      _controllerOf(tester).trimFrom(1);
      await tester.pump();
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(_sentTo('/track/encrypted'), hasLength(1));
      expect(_sentTo('/activities/$_id/track'), isEmpty);
      expect(find.byType(ActivityEditorPage), findsNothing);
    });

    testWidgets("a plaintext activity's save request is unchanged", (tester) async {
      final points = _openedPoints();
      final activity = {
        'id': _id,
        'name': 'Ride',
        'map': {'summary_polyline': _polyline},
        'elevation_profile': [
          for (var i = 0; i < _profile.distancesKm.length; i++)
            [_profile.distancesKm[i], _profile.elevationsM[i]],
        ],
        'lock_version': _lockVersion,
      };
      await _pumpEditor(
          tester, ActivityEditorPage(notifier: _notifier(), activity: activity));

      _controllerOf(tester).trimFrom(1);
      await tester.pump();
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      final put = _sentTo('/activities/$_id/track').single;
      expect(_body(put), {
        'points': [for (final p in points.sublist(1)) p.toJson()],
        'lock_version': _lockVersion,
      });
      expect(_sentTo('/track/encrypted'), isEmpty);
    });

    testWidgets("a plaintext activity's split request is unchanged", (tester) async {
      // Points a kilometre apart, so each vertex handle can be pressed alone.
      final line = [for (var i = 0; i < 6; i++) (48.0, 2.0 + i * 0.01)];
      final activity = {
        'id': _id,
        'name': 'Ride',
        'map': {'summary_polyline': encodePolyline(line)},
        'lock_version': _lockVersion,
      };
      await _pumpEditor(
          tester, ActivityEditorPage(notifier: _notifier(), activity: activity));

      await tester.longPress(find.byKey(const ValueKey('vertex_3')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Split here'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Split'));
      await tester.pumpAndSettle();

      final post = _sentTo('/activities/$_id/split').single;
      expect(_body(post), {
        'split_index': 3,
        'drop_boundary': false,
        'points': [for (final (lat, lng) in line) {'lat': lat, 'lng': lng}],
        'lock_version': _lockVersion,
      });
      expect(_sentTo('/split/encrypted'), isEmpty);
    });
  });

  group('reset', () {
    Map<String, dynamic> edited() => {
          'id': _id,
          'name': 'Ride',
          'is_edited': true,
          'map': {'summary_polyline': _polyline},
          'lock_version': _lockVersion,
        };

    Future<void> reset(WidgetTester tester) async {
      await _pumpEditor(
          tester, ActivityEditorPage(notifier: _notifier(), activity: edited()));
      await tester.tap(find.text('Reset to Strava'));
      await tester.pumpAndSettle();
    }

    http.Response Function(http.Request) refuse(String code) => (req) =>
        req.url.path.endsWith('/reset')
            ? http.Response(
                jsonEncode({
                  'detail': {'code': code, 'message': 'refused', 'activity_id': _id},
                }),
                409)
            : _ok(req);

    testWidgets("sends the editor's lock_version", (tester) async {
      await reset(tester);

      expect(_body(_sentTo('/activities/$_id/reset').single),
          {'lock_version': _lockVersion});
      expect(find.byType(ActivityEditorPage), findsNothing);
    });

    testWidgets('nothing_to_restore is explained and the editor stays open',
        (tester) async {
      _answer = refuse('nothing_to_restore');
      await reset(tester);

      expect(find.text("This activity's original track was not kept, so it "
          "can't be reset."), findsOneWidget);
      expect(find.byType(ActivityEditorPage), findsOneWidget);
    });

    testWidgets('stale_write is explained and closes the editor', (tester) async {
      _answer = refuse('stale_write');
      await reset(tester);

      expect(find.textContaining('This activity changed elsewhere.'), findsOneWidget);
      expect(find.byType(ActivityEditorPage), findsNothing);
    });
  });
}
