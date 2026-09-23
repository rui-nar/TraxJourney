/// Parsing the inspect response (issue #260, unit 5).
///
/// Separate from the dialog tests because the timezone question here decides
/// what gets SENT BACK, and a bug in it moves an activity by hours without
/// anything on screen looking wrong.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/gpx_import_model.dart';

Map<String, dynamic> _candidate([Map<String, dynamic> overrides = const {}]) => {
      'index': 0,
      'name': 'Morning ride',
      'activity_type': 'ride',
      'point_count': 12480,
      'distance_m': 42300.0,
      'is_route': false,
      'has_times': true,
      'started_at': '2024-08-12T07:33:12+00:00',
      'ended_at': '2024-08-12T10:37:00+00:00',
      'elapsed_seconds': 11028,
      'moving_seconds': 9810,
      'elevation_gain_m': 610.0,
      'elevation_gain_estimated': true,
      'polyline': 'ab~bGgcoeA??',
      'errors': <String>[],
      ...overrides,
    };

void main() {
  group('times stay on one clock', () {
    test('a parsed time is UTC, whatever zone the device is in', () {
      // The dialog turns these into the HH:MM it displays AND into the HH:MM it
      // posts back, and the server reads that as UTC. Converting to local here
      // put 09:33 in the field for a 07:33Z ride and then sent 09:33 as though
      // it were UTC, moving the activity by the offset. Asserting isUtc catches
      // that on every machine, including a runner that happens to be on UTC and
      // where the hour alone would look right.
      final candidate = GpxCandidate.fromJson(_candidate());

      expect(candidate.startedAt!.isUtc, isTrue);
      expect(candidate.endedAt!.isUtc, isTrue);
      expect(candidate.startedAt!.hour, 7);
      expect(candidate.startedAt!.minute, 33);
    });

    test('an offset in the file is normalised, not carried', () {
      final candidate = GpxCandidate.fromJson(
          _candidate({'started_at': '2024-08-12T09:33:12+02:00'}));

      expect(candidate.startedAt!.isUtc, isTrue);
      expect(candidate.startedAt!.hour, 7);
    });

    test('a route has no times at all', () {
      final candidate = GpxCandidate.fromJson(_candidate({
        'started_at': null,
        'ended_at': null,
        'has_times': false,
        'moving_seconds': null,
      }));

      expect(candidate.startedAt, isNull);
      expect(candidate.hasTimes, isFalse);
    });
  });

  group('degrading rather than throwing', () {
    test('a whole number where a double is expected', () {
      final candidate = GpxCandidate.fromJson(
          _candidate({'distance_m': 42300, 'elevation_gain_m': 610}));

      expect(candidate.distanceM, 42300);
      expect(candidate.elevationGainM, 610);
    });

    test('missing optional keys', () {
      final candidate = GpxCandidate.fromJson({'index': 0, 'errors': []});

      expect(candidate.name, isNull);
      expect(candidate.activityType, isNull);
      expect(candidate.pointCount, 0);
      expect(candidate.outline, isEmpty);
      expect(candidate.isImportable, isTrue);
    });

    test('a malformed polyline costs the thumbnail, not the import', () {
      final candidate =
          GpxCandidate.fromJson(_candidate({'polyline': '!!!not-a-polyline'}));

      expect(candidate.outline, isEmpty);
      expect(candidate.isImportable, isTrue);
    });

    test('an unparseable time is absent rather than wrong', () {
      final candidate =
          GpxCandidate.fromJson(_candidate({'started_at': '12 August, 7am'}));

      expect(candidate.startedAt, isNull);
    });
  });

  group('what the dialog asks of an inspection', () {
    test('several importable tracks need a choice', () {
      final inspection = GpxInspection.fromJson({
        'candidates': [_candidate(), _candidate({'index': 1})],
        'errors': [],
      });

      expect(inspection.needsAChoice, isTrue);
    });

    test('one importable track among unusable ones needs no choice', () {
      final inspection = GpxInspection.fromJson({
        'candidates': [
          _candidate(),
          _candidate({'index': 1, 'errors': ['Track has fewer than 2 points (1).']}),
        ],
        'errors': [],
      });

      expect(inspection.needsAChoice, isFalse);
    });

    test('a duplicate is read out when present, and absent otherwise', () {
      final withDuplicate = GpxInspection.fromJson({
        'candidates': [_candidate()],
        'duplicate_of': {'activity_id': -7, 'name': 'Morning ride'},
        'errors': [],
      });
      final without =
          GpxInspection.fromJson({'candidates': [_candidate()], 'errors': []});

      expect(withDuplicate.duplicateOf!.activityId, -7);
      expect(without.duplicateOf, isNull);
    });
  });
}
