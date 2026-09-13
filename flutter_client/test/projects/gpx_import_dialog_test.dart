/// Tests for GpxImportDialog — the pick → review → confirm flow (issue #260).
///
/// The dialog's whole reason for existing is that the user should confirm what
/// the file says rather than transcribe it, and should never meet a rejection
/// after filling in a form. So there is a test per state the flow can be in:
/// reading, a choice between tracks, a prefilled review, a route with no clock,
/// a duplicate, a date outside the trip, a rejection, and a success.
library;

import 'dart:convert';
import 'dart:typed_data';

import 'package:cross_file/cross_file.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/gpx_import_dialog.dart';

const _ref = ProjectRef(name: 'Trip');

final Uint8List _gpxBytes = utf8.encode('<gpx></gpx>');

/// A short encoded polyline, so the preview has something to draw.
const _outline = 'ab~bGgcoeA??';

Map<String, dynamic> _candidate({
  int index = 0,
  String? name = 'Morning ride',
  String? type = 'ride',
  bool isRoute = false,
  bool hasTimes = true,
  String? startedAt = '2024-08-12T07:33:12Z',
  String? endedAt = '2024-08-12T10:37:00Z',
  int? movingSeconds = 9810,
  double? gain = 610,
  List<String> errors = const [],
}) =>
    {
      'index': index,
      'name': name,
      'activity_type': type,
      'point_count': 12480,
      'distance_m': 42300.0,
      'is_route': isRoute,
      'has_times': hasTimes,
      'started_at': startedAt,
      'ended_at': endedAt,
      'elapsed_seconds': 11028,
      'moving_seconds': movingSeconds,
      'elevation_gain_m': gain,
      'elevation_gain_estimated': true,
      'polyline': errors.isEmpty ? _outline : null,
      'errors': errors,
    };

String _inspectBody({
  List<Map<String, dynamic>>? candidates,
  String? suggestedName = 'Morning ride',
  Map<String, dynamic>? duplicateOf,
  List<String> errors = const [],
}) =>
    jsonEncode({
      'candidates': candidates ?? [_candidate()],
      'suggested_name': suggestedName,
      'duplicate_of': duplicateOf,
      'errors': errors,
    });

/// Fakes the file_picker plugin so tests can pick a .gpx file without a real
/// platform channel — mirrors memory_dialog_save_result_test.dart's fake.
class _FakeFilePickerPlatform extends FilePickerPlatform {
  _FakeFilePickerPlatform(this.filesToReturn);
  final List<PlatformFile> Function() filesToReturn;

  @override
  Future<List<PlatformFile>> pickFiles({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    Function(FilePickerStatus)? onFileLoading,
    int compressionQuality = 0,
    AndroidOptions androidOptions = const AndroidOptions(),
    WindowsOptions windowsOptions = const WindowsOptions(),
    LinuxOptions linuxOptions = const LinuxOptions(),
    WebOptions webOptions = const WebOptions(),
  }) async =>
      filesToReturn();

  @override
  Future<PlatformFile?> pickFile({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    Function(FilePickerStatus)? onFileLoading,
    int compressionQuality = 0,
    AndroidOptions androidOptions = const AndroidOptions(),
    WindowsOptions windowsOptions = const WindowsOptions(),
    LinuxOptions linuxOptions = const LinuxOptions(),
    WebOptions webOptions = const WebOptions(),
  }) async {
    final files = filesToReturn();
    return files.isEmpty ? null : files.first;
  }
}

/// file_picker 12 made [PlatformFile] abstract and moved the bytes behind
/// readAsBytes(), so tests supply their own in-memory file rather than
/// constructing one from a byte list.
final class _FakePlatformFile extends PlatformFile {
  _FakePlatformFile({required this.name, required this.bytes});

  @override
  final String name;

  final Uint8List bytes;

  @override
  Uri get uri => Uri.dataFromBytes(bytes);

  @override
  XFile get xFile => XFile.fromData(bytes, name: name);

  @override
  Future<int> length() async => bytes.length;

  @override
  Future<Uint8List> readAsBytes() async => bytes;

  @override
  Stream<Uint8List> readAsByteStream() => Stream.value(bytes);
}

/// Records what the dialog sent, so a test can assert on the request as well as
/// on the pixels.
class _Recorder {
  final List<http.BaseRequest> requests = [];
  final List<String> bodies = [];
  GpxImportResult? result;

  http.Client client({
    String inspect = '',
    int inspectStatus = 200,
    String import = '{"activity_id": -42, "total": 3}',
    int importStatus = 200,
  }) =>
      MockClient((request) async {
        requests.add(request);
        // MockClient hands the handler a materialised Request, not the
        // MultipartRequest that was sent, so the fields have to be read
        // back out of the encoded body.
        bodies.add(request.body);
        final isInspect = request.url.path.endsWith('/gpx/inspect');
        return http.Response(
          isInspect ? (inspect.isEmpty ? _inspectBody() : inspect) : import,
          isInspect ? inspectStatus : importStatus,
        );
      });

  /// One field's value, pulled out of the last multipart body.
  String? field(String name) =>
      RegExp('name="$name"' r'\r?\n\r?\n([^\r\n]*)')
          .firstMatch(bodies.last)
          ?.group(1);

  bool sent(String name) => bodies.last.contains('name="$name"');
}

Widget _harness(
  _Recorder recorder, {
  http.Client? client,
  String? tripStart,
  String? tripEnd,
}) =>
    MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () async {
              recorder.result = await showDialog<GpxImportResult>(
                context: context,
                builder: (_) => GpxImportDialog(
                  projectRef: _ref,
                  httpClient: client ?? recorder.client(),
                  tripStart: tripStart,
                  tripEnd: tripEnd,
                ),
              );
            },
            child: const Text('open'),
          ),
        ),
      ),
    );

Future<void> _openAndPick(WidgetTester tester) async {
  FilePickerPlatform.instance = _FakeFilePickerPlatform(
    () => [_FakePlatformFile(name: '2024-08-12_073312.gpx', bytes: _gpxBytes)],
  );
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const ValueKey('gpx_pick_file')));
  await tester.pumpAndSettle();
}

ElevatedButton _confirmButton(WidgetTester tester) => tester
    .widget<ElevatedButton>(find.byKey(const ValueKey('gpx_import_confirm')));

void main() {
  testWidgets('the file is read as soon as it is picked', (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder));
    await _openAndPick(tester);

    expect(recorder.requests.first.url.path,
        endsWith('/api/projects/Trip/activities/gpx/inspect'));
    expect(find.text('Import this track?'), findsOneWidget);
  });

  testWidgets('every field arrives filled in from the file', (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder));
    await _openAndPick(tester);

    // The name comes from the track, not from the device's filename — which is
    // the whole point of reading the file before asking anything.
    expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('gpx_name_field')))
            .controller!
            .text,
        'Morning ride');
    expect(find.byKey(const ValueKey('gpx_from_file_note')), findsOneWidget);
    expect(find.text('Required'), findsNothing);
    expect(find.byKey(const ValueKey('gpx_climb_estimated')), findsOneWidget);
    expect(find.textContaining('42.3 km'), findsOneWidget);
    expect(find.textContaining('610 m'), findsOneWidget);
  });

  testWidgets('untouched times are not echoed back to the server',
      (tester) async {
    // The form carries only HH:MM and the file's start has seconds in it, so
    // sending the truncated value back changes the fingerprint the server
    // dedupes on — and a re-import of the same file would not be recognised.
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder));
    await _openAndPick(tester);

    await tester.tap(find.byKey(const ValueKey('gpx_import_confirm')));
    await tester.pumpAndSettle();

    expect(recorder.sent('date'), isFalse);
    expect(recorder.sent('start_time'), isFalse);
    expect(recorder.field('activity_name'), 'Morning ride');
    expect(recorder.field('activity_type'), 'ride');
    expect(recorder.field('track_index'), '0');
  });

  testWidgets('a planned route asks for the date it has not got',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [
        _candidate(
            name: 'Planned loop',
            type: null,
            isRoute: true,
            hasTimes: false,
            startedAt: null,
            endedAt: null,
            movingSeconds: null),
      ])),
    ));
    await _openAndPick(tester);

    expect(find.byKey(const ValueKey('gpx_route_notice')), findsOneWidget);
    expect(find.text('Required'), findsNWidgets(3)); // date, start, end
    expect(_confirmButton(tester).onPressed, isNull,
        reason: 'nothing to submit until the missing facts are supplied');
  });

  testWidgets('a file with several tracks asks which one', (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [
        _candidate(index: 0, name: 'Lap 1'),
        _candidate(index: 1, name: 'Lap 2'),
      ])),
    ));
    await _openAndPick(tester);

    expect(find.text('Which track?'), findsOneWidget);
    expect(find.text('Lap 1'), findsOneWidget);
    expect(find.text('Lap 2'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('gpx_candidate_1')));
    await tester.pumpAndSettle();

    expect(find.text('Import this track?'), findsOneWidget);
  });

  testWidgets('a chosen track is the one submitted', (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [
        _candidate(index: 0, name: 'Lap 1'),
        _candidate(index: 1, name: 'Lap 2'),
      ])),
    ));
    await _openAndPick(tester);
    await tester.tap(find.byKey(const ValueKey('gpx_candidate_1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('gpx_import_confirm')));
    await tester.pumpAndSettle();

    expect(recorder.field('track_index'), '1');
    expect(recorder.field('activity_name'), 'Lap 2');
  });

  testWidgets('an unusable track is offered but cannot be chosen',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [
        _candidate(index: 0, name: 'Lap 1'),
        _candidate(index: 1, name: 'Lap 2'),
        _candidate(
            index: 2,
            name: 'Empty one',
            errors: const ['Track has fewer than 2 points (1).']),
      ])),
    ));
    await _openAndPick(tester);

    // Shown with its reason rather than hidden: a user who knows the file has
    // three tracks should not be silently offered two.
    expect(find.text('Track has fewer than 2 points (1).'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('gpx_candidate_2')));
    await tester.pumpAndSettle();

    expect(find.text('Which track?'), findsOneWidget,
        reason: 'tapping the unusable one must do nothing');
  });

  testWidgets('one usable track among several is not a question worth asking',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [
        _candidate(index: 0, name: 'The real one'),
        _candidate(
            index: 1,
            name: 'Empty one',
            errors: const ['Track has fewer than 2 points (1).']),
      ])),
    ));
    await _openAndPick(tester);

    expect(find.text('Import this track?'), findsOneWidget);
    expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('gpx_name_field')))
            .controller!
            .text,
        'The real one');
  });

  testWidgets('a track already in the trip is flagged before importing',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(
              duplicateOf: {'activity_id': -7, 'name': 'Morning ride'})),
    ));
    await _openAndPick(tester);

    expect(find.byKey(const ValueKey('gpx_duplicate_notice')), findsOneWidget);
  });

  testWidgets('a rejected file says why, and stays on the pick step',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: jsonEncode({
            'detail': {
              'errors': [
                'GPX contains only waypoints, and no route or track to import.'
              ]
            }
          }),
          inspectStatus: 422),
    ));
    await _openAndPick(tester);

    expect(find.textContaining('only waypoints'), findsOneWidget);
    expect(find.byKey(const ValueKey('gpx_pick_file')), findsOneWidget);
  });

  testWidgets('a date outside the trip is flagged but not blocked',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder,
        tripStart: '2024-09-01', tripEnd: '2024-09-14'));
    await _openAndPick(tester);

    // The file's own date, 12 August, is before this trip starts.
    expect(
        find.byKey(const ValueKey('gpx_outside_trip_notice')), findsOneWidget);
    expect(_confirmButton(tester).onPressed, isNotNull,
        reason: 'importing a ride from the day before a trip is legitimate');
  });

  testWidgets('a date inside the trip is not flagged', (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder,
        tripStart: '2024-08-01', tripEnd: '2024-08-31'));
    await _openAndPick(tester);

    expect(find.byKey(const ValueKey('gpx_outside_trip_notice')), findsNothing);
  });

  testWidgets('a successful import hands back what was created',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder));
    await _openAndPick(tester);
    await tester.tap(find.byKey(const ValueKey('gpx_import_confirm')));
    await tester.pumpAndSettle();

    expect(recorder.result, isNotNull);
    expect(recorder.result!.activityId, -42);
    expect(recorder.result!.name, 'Morning ride');
  });

  testWidgets('a refused import returns to the review step with the reason',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          import: jsonEncode({
            'detail': {
              'errors': [
                'This trip already has "Morning ride" from the same track.'
              ],
              'activity_id': -7
            }
          }),
          importStatus: 409),
    ));
    await _openAndPick(tester);
    await tester.tap(find.byKey(const ValueKey('gpx_import_confirm')));
    await tester.pumpAndSettle();

    expect(find.textContaining('already has'), findsOneWidget);
    expect(find.text('Import this track?'), findsOneWidget);
  });

  testWidgets('an empty name cannot be submitted', (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder));
    await _openAndPick(tester);

    await tester.enterText(find.byKey(const ValueKey('gpx_name_field')), '   ');
    await tester.pump();

    expect(_confirmButton(tester).onPressed, isNull);
  });

  testWidgets('the dialog fits a narrow phone', (tester) async {
    // setSurfaceSize rather than an outer MediaQuery: MaterialApp builds
    // its own from the view, so a wrapper is simply discarded.
    await tester.binding.setSurfaceSize(const Size(360, 700));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder));
    await _openAndPick(tester);

    expect(tester.takeException(), isNull);
    expect(tester.getSize(find.byKey(const ValueKey('gpx_name_field'))).width,
        lessThan(360));
  });

  testWidgets('a ride over midnight is a ride, not an error', (tester) async {
    // Nothing the user does triggers this: the file's own span is 22:30 to
    // 01:10, and comparing minutes-past-midnight refused it outright while the
    // form still said "read from the file".
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [
        _candidate(
            startedAt: '2024-08-12T22:30:00Z',
            endedAt: '2024-08-13T01:10:00Z'),
      ])),
    ));
    await _openAndPick(tester);

    expect(find.byKey(const ValueKey('gpx_crosses_midnight_note')),
        findsOneWidget);
    expect(find.textContaining('cannot be the same time'), findsNothing);
    expect(_confirmButton(tester).onPressed, isNotNull,
        reason: 'a night ride must be importable');
  });

  testWidgets('an edited time is sent as the same clock the server reads',
      (tester) async {
    // The field shows, and posts, the file's own UTC wall time. Showing the
    // device's local time instead sent 09:33 for a 07:33Z ride, which the
    // server stores as 09:33Z — the activity moves by the offset, silently.
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(recorder));
    await _openAndPick(tester);

    // Touch the date and accept it: enough to flip the form to "edited".
    await tester.tap(find.byKey(const ValueKey('gpx_date_field')));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'OK'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('gpx_import_confirm')));
    await tester.pumpAndSettle();

    expect(recorder.field('start_time'), '07:33');
    expect(recorder.field('end_time'), '10:37');
    expect(recorder.field('date'), '2024-08-12');
  });

  testWidgets('a file whose only track is unusable says why, on the pick step',
      (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [
        _candidate(errors: const ['Track has fewer than 2 points (1).']),
      ])),
    ));
    await _openAndPick(tester);

    expect(find.text('Track has fewer than 2 points (1).'), findsOneWidget);
    expect(find.byKey(const ValueKey('gpx_pick_file')), findsOneWidget,
        reason: 'a review step that can never be submitted is a dead end');
  });

  testWidgets('a server that cannot be reached leaves a way out',
      (tester) async {
    // The spinner used to keep turning with Cancel disabled, and the only
    // escape was tapping the barrier.
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: MockClient((_) async => throw http.ClientException('offline')),
    ));
    await _openAndPick(tester);

    expect(find.textContaining('Could not reach the server'), findsOneWidget);
    expect(
        tester
            .widget<TextButton>(find.widgetWithText(TextButton, 'Cancel'))
            .onPressed,
        isNotNull);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('a failed import leaves the review step usable', (tester) async {
    final recorder = _Recorder();
    var calls = 0;
    await tester.pumpWidget(_harness(
      recorder,
      client: MockClient((request) async {
        calls++;
        if (request.url.path.endsWith('/gpx/inspect')) {
          return http.Response(_inspectBody(), 200);
        }
        throw http.ClientException('offline');
      }),
    ));
    await _openAndPick(tester);
    await tester.tap(find.byKey(const ValueKey('gpx_import_confirm')));
    await tester.pumpAndSettle();

    expect(calls, 2);
    expect(find.text('Import this track?'), findsOneWidget);
    expect(_confirmButton(tester).onPressed, isNotNull,
        reason: 'the user must be able to try again');
  });

  testWidgets('an unrecognised type says what it needs', (tester) async {
    final recorder = _Recorder();
    await tester.pumpWidget(_harness(
      recorder,
      client: recorder.client(
          inspect: _inspectBody(candidates: [_candidate(type: null)])),
    ));
    await _openAndPick(tester);

    // An Import button sitting dead with no explanation is its own small
    // cruelty; every other missing field says "Required".
    expect(find.textContaining("The file doesn't say"), findsOneWidget);
    expect(_confirmButton(tester).onPressed, isNull);
  });
}
