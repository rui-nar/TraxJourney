import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/encryption.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/activity_editor_page.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/track_editor_controller.dart';
import 'package:traxjourney_client/src/track_metrics/polyline_encoder.dart';

import '../crypto/encryption_service_test.dart' show FakeDeviceKeyStore, FakeEncryptionApi;

/// The track editor's open path for an encrypted activity (E2EE remnants
/// decision 7, issue #29 before it): an encrypted track is always opened from
/// `GET …/track`, decrypted, even when the panel's copy has a polyline — the
/// panel's copy is revealed in place, so it can look plaintext. A device that
/// can't decrypt it gets a message instead of an editor that can't save.
/// Plaintext activities on an account without encryption open as before.
///
/// The app's real `encryption` singleton is used; it talks to the server
/// through the `api` client current when it is first used, so every test
/// shares one client whose answers [_track] decides.

const _ref = ProjectRef(name: 'Trip');

final _panelLine =
    encodePolyline([(48.0, 2.0), (48.0, 2.01), (48.0, 2.02), (48.0, 2.03)]);
final _storedLine = encodePolyline(
    [for (var i = 0; i < 6; i++) (45.83 + i * 0.0002, 6.86 + i * 0.0002)]);

/// The `GET …/track` answer, and every request sent.
Map<String, dynamic> _track = {};
final List<http.Request> _requests = [];

List<http.Request> get _trackFetches =>
    [for (final r in _requests) if (r.url.path.endsWith('/activities/42/track')) r];

ProjectNotifier _notifierWithOneActivity(Map<String, dynamic> activity) {
  final n = ProjectNotifier(ProjectService())..ref = _ref;
  n.activities = [
    {
      'id': 42,
      'type': 'Ride',
      'name': 'Ride',
      'distance': 5000,
      'moving_time': 1800,
      'start_date_local': '2026-06-01T08:00:00',
      'elevation_profile': [
        [0.0, 10.0],
        [1.0, 20.0],
      ],
      ...activity,
    },
  ];
  n.items = [
    {'item_type': 'activity', 'activity_id': 42},
  ];
  return n;
}

Future<void> _pumpPanel(WidgetTester tester, ProjectNotifier notifier) async {
  tester.view.physicalSize = const Size(1200, 900);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(
    ChangeNotifierProvider<ProjectNotifier>.value(
      value: notifier,
      child: MaterialApp(
        home: Scaffold(body: ActivityPanel(notifier: notifier)),
      ),
    ),
  );
  // Days start collapsed; expand so the activity row (and its edit icon) renders.
  await tester.tap(find.byIcon(Icons.unfold_more));
  await tester.pumpAndSettle();
}

/// Taps "Edit track", letting the fetch and the decryption run for real.
Future<void> _editTrack(WidgetTester tester) async {
  await tester.runAsync(() async {
    await tester.tap(find.byTooltip('Edit track'));
    await Future<void>.delayed(const Duration(milliseconds: 300));
  });
  await tester.pumpAndSettle();
}

/// The stored track of activity 42, encrypted under [svc]'s key.
Future<Map<String, dynamic>> _encryptedTrack(EncryptionService svc) async => {
      'id': 42,
      'name': await svc.encryptText('Ride'),
      'is_edited': false,
      'moving_time': 1800,
      'elapsed_time': 2000,
      'total_elevation_gain': 30.0,
      'map': {'summary_polyline': await svc.encryptText(_storedLine)},
      'elevation_profile': null,
      'elevation_profile_enc': null,
      'lock_version': 5,
    };

const _lockedMessage = "This activity's track is encrypted. Unlock encryption "
    'on this device (approve it, or recover access) to edit it.';

/// For an account without encryption there is nothing to unlock (U14-R1-1).
const _encryptedMessage =
    "This activity is encrypted and its track can't be edited.";

void main() {
  setUpAll(() {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          _requests.add(req);
          if (req.url.path.endsWith('/activities/42/track')) {
            return http.Response(jsonEncode(_track), 200);
          }
          return http.Response('{}', 200);
        }));
  });

  setUp(() {
    _requests.clear();
    _track = {};
  });

  group('on an account without encryption', () {
    testWidgets(
        'an encrypted track shows a message and does NOT open the editor',
        (tester) async {
      final notifier = _notifierWithOneActivity({
        'map': {'summary_polyline': 'v1.d2VsY29tZQ==.Y2lwaGVy'},
      });
      await _pumpPanel(tester, notifier);

      await tester.tap(find.byTooltip('Edit track'));
      await tester.pump(); // let the SnackBar animation start

      expect(find.byType(ActivityEditorPage), findsNothing);
      expect(find.text(_encryptedMessage), findsOneWidget);
      expect(_trackFetches, isEmpty);
    });

    testWidgets('a plaintext activity opens the editor from the panel copy',
        (tester) async {
      final notifier =
          _notifierWithOneActivity({'map': {'summary_polyline': _panelLine}});
      await _pumpPanel(tester, notifier);

      await tester.tap(find.byTooltip('Edit track'));
      await tester.pumpAndSettle();

      expect(find.byType(ActivityEditorPage), findsOneWidget);
      expect(_trackFetches, isEmpty);
    });

    testWidgets('a track fetched as an envelope shows the message too',
        (tester) async {
      final other = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await tester.runAsync(() async {
        await other.enable(const RecoveryKeyChoice());
        _track = await _encryptedTrack(other);
      });
      final notifier = _notifierWithOneActivity({});
      await _pumpPanel(tester, notifier);

      await _editTrack(tester);

      expect(_trackFetches, hasLength(1));
      expect(find.byType(ActivityEditorPage), findsNothing);
      expect(find.text(_encryptedMessage), findsOneWidget);
    });
  });

  group('with encryption unlocked', () {
    setUp(() async {
      FlutterSecureStorage.setMockInitialValues({});
      await encryption.enable(const RecoveryKeyChoice());
      expect(encryption.isUnlocked, isTrue);
    });
    tearDown(() => encryption.lock());

    testWidgets(
        'an encrypted activity is opened from GET …/track even when the '
        "panel's copy has a polyline", (tester) async {
      await tester.runAsync(() async => _track = await _encryptedTrack(encryption));
      // The panel's copy, revealed in place: a plaintext polyline that is not
      // the stored track.
      final notifier = _notifierWithOneActivity({
        'map': {'summary_polyline': _panelLine},
      });
      await _pumpPanel(tester, notifier);

      await _editTrack(tester);

      expect(_trackFetches, hasLength(1));
      final page = tester.widget<ActivityEditorPage>(find.byType(ActivityEditorPage));
      expect(page.encrypted, isNotNull);
      expect(page.encrypted!.polyline, _storedLine);
      expect(page.encrypted!.lockVersion, 5);
      final state = tester.state<State>(find.byType(ActivityEditorPage));
      final c = (state as dynamic).editorControllerForTest as TrackEditorController;
      expect(c.points, hasLength(6));
      expect(c.points.first.lat, closeTo(45.83, 1e-9));
    });

    testWidgets('a plaintext track on an encrypted account opens as before',
        (tester) async {
      _track = {
        'id': 42,
        'name': 'Ride',
        'map': {'summary_polyline': _storedLine},
        'lock_version': 5,
      };
      final notifier =
          _notifierWithOneActivity({'map': {'summary_polyline': _panelLine}});
      await _pumpPanel(tester, notifier);

      await _editTrack(tester);

      expect(_trackFetches, hasLength(1));
      final page = tester.widget<ActivityEditorPage>(find.byType(ActivityEditorPage));
      expect(page.encrypted, isNull);
      expect(page.activity['lock_version'], 5);
    });

    testWidgets("a track under another account's key says so", (tester) async {
      final other = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await tester.runAsync(() async {
        await other.enable(const RecoveryKeyChoice());
        _track = await _encryptedTrack(other);
      });
      final notifier = _notifierWithOneActivity({});
      await _pumpPanel(tester, notifier);

      await _editTrack(tester);

      expect(find.byType(ActivityEditorPage), findsNothing);
      expect(
          find.text("This activity's track is encrypted with a key this "
              "device doesn't have, so it can't be edited here."),
          findsOneWidget);
    });
  });

  group('with encryption locked on this device', () {
    setUp(() async {
      FlutterSecureStorage.setMockInitialValues({});
      await encryption.enable(const RecoveryKeyChoice());
      encryption.lock();
      expect(encryption.state.value, EncryptionState.locked);
    });

    testWidgets('an encrypted track says to unlock, without fetching',
        (tester) async {
      final notifier = _notifierWithOneActivity({
        'map': {'summary_polyline': 'v1.d2VsY29tZQ==.Y2lwaGVy'},
      });
      await _pumpPanel(tester, notifier);

      await tester.tap(find.byTooltip('Edit track'));
      await tester.pump();

      expect(find.byType(ActivityEditorPage), findsNothing);
      expect(find.text(_lockedMessage), findsOneWidget);
      expect(find.text(_encryptedMessage), findsNothing);
      expect(_trackFetches, isEmpty);
    });

    testWidgets('a track fetched as an envelope says to unlock too',
        (tester) async {
      final other = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await tester.runAsync(() async {
        await other.enable(const RecoveryKeyChoice());
        _track = await _encryptedTrack(other);
      });
      final notifier = _notifierWithOneActivity({});
      await _pumpPanel(tester, notifier);

      await _editTrack(tester);

      expect(_trackFetches, hasLength(1));
      expect(find.byType(ActivityEditorPage), findsNothing);
      expect(find.text(_lockedMessage), findsOneWidget);
    });
  });
}
