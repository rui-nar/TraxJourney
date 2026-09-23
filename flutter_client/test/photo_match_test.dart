import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/photos/photo_match.dart';

void main() {
  group('selectCandidatesForDay — day boundary', () {
    // Trip local time is US Pacific (UTC-7). A naive UTC-calendar-date
    // comparison would misclassify these because 23:50 local on the 10th is
    // already the 11th in UTC, and 00:05 local on the 11th is still the 10th
    // in UTC — exactly the off-by-one trap the brief calls out.
    const pacificOffset = Duration(hours: -7);

    test('candidate just before local midnight is included', () {
      final candidates = [
        PhotoCandidate(
          capturedAt: DateTime.utc(2026, 7, 11, 6, 50), // 2026-07-10 23:50 PDT
        ),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: pacificOffset,
        candidates: candidates,
      );
      expect(result, hasLength(1));
    });

    test('candidate just after local midnight belongs to the next day', () {
      final candidates = [
        PhotoCandidate(
          capturedAt: DateTime.utc(2026, 7, 11, 7, 5), // 2026-07-11 00:05 PDT
        ),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: pacificOffset,
        candidates: candidates,
      );
      expect(result, isEmpty);
    });

    test('candidate exactly at local midnight belongs to the next day', () {
      final candidates = [
        PhotoCandidate(
          capturedAt: DateTime.utc(2026, 7, 11, 7, 0), // 2026-07-11 00:00 PDT
        ),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: pacificOffset,
        candidates: candidates,
      );
      expect(result, isEmpty);
    });

    test('candidate on an unrelated day is excluded', () {
      final candidates = [
        PhotoCandidate(capturedAt: DateTime.utc(2026, 7, 9, 12, 0)),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: pacificOffset,
        candidates: candidates,
      );
      expect(result, isEmpty);
    });
  });

  group('selectCandidatesForDay — geo tolerance', () {
    const zeroOffset = Duration.zero;
    final sameDay = DateTime.utc(2026, 7, 10, 12, 0);

    test('candidate inside the radius is included', () {
      final candidates = [
        // ~5.5km north of the memory location.
        PhotoCandidate(capturedAt: sameDay, lat: 48.05, lon: 2.0),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidates: candidates,
        memoryLat: 48.0,
        memoryLon: 2.0,
        geoToleranceKm: 50,
      );
      expect(result, hasLength(1));
    });

    test('candidate outside the radius is excluded', () {
      final candidates = [
        // ~111km north of the memory location.
        PhotoCandidate(capturedAt: sameDay, lat: 49.0, lon: 2.0),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidates: candidates,
        memoryLat: 48.0,
        memoryLon: 2.0,
        geoToleranceKm: 50,
      );
      expect(result, isEmpty);
    });

    test('memory with no lat/lon does not filter by geo at all', () {
      final candidates = [
        PhotoCandidate(capturedAt: sameDay, lat: 60.0, lon: 100.0),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidates: candidates,
        memoryLat: null,
        memoryLon: null,
      );
      expect(result, hasLength(1));
    });

    test('candidate with no GPS is still included by day-only match', () {
      final candidates = [
        PhotoCandidate(capturedAt: sameDay),
      ];
      final result = selectCandidatesForDay(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidates: candidates,
        memoryLat: 48.0,
        memoryLon: 2.0,
        geoToleranceKm: 50,
      );
      expect(result, hasLength(1));
    });
  });

  group('classifyDayGeoMatch', () {
    const zeroOffset = Duration.zero;
    final sameDay = DateTime.utc(2026, 7, 10, 12, 0);

    test('right day, no geo to check -> none', () {
      final result = classifyDayGeoMatch(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidate: PhotoCandidate(capturedAt: sameDay),
      );
      expect(result, DayGeoMismatch.none);
    });

    test('right day, within geo tolerance -> none', () {
      final result = classifyDayGeoMatch(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidate: PhotoCandidate(capturedAt: sameDay, lat: 48.05, lon: 2.0),
        memoryLat: 48.0,
        memoryLon: 2.0,
      );
      expect(result, DayGeoMismatch.none);
    });

    test('wrong day is reported as wrongDay, not tooFarAway, even when the '
        'candidate also has no geo to check', () {
      final result = classifyDayGeoMatch(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidate: PhotoCandidate(capturedAt: DateTime.utc(2026, 7, 9, 12, 0)),
      );
      expect(result, DayGeoMismatch.wrongDay);
    });

    test('right day but >tolerance away is reported as tooFarAway, not '
        'wrongDay — this is the case the UI used to mislabel', () {
      final result = classifyDayGeoMatch(
        date: '2026-07-10',
        localOffset: zeroOffset,
        // ~111km north of the memory location.
        candidate: PhotoCandidate(capturedAt: sameDay, lat: 49.0, lon: 2.0),
        memoryLat: 48.0,
        memoryLon: 2.0,
        geoToleranceKm: 50,
      );
      expect(result, DayGeoMismatch.tooFarAway);
    });

    test('wrong day AND far away is still reported as wrongDay (day check '
        'short-circuits before geo is even considered)', () {
      final result = classifyDayGeoMatch(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidate: PhotoCandidate(
            capturedAt: DateTime.utc(2026, 7, 9, 12, 0), lat: 49.0, lon: 2.0),
        memoryLat: 48.0,
        memoryLon: 2.0,
        geoToleranceKm: 50,
      );
      expect(result, DayGeoMismatch.wrongDay);
    });

    test('memory with no lat/lon does not flag geo at all', () {
      final result = classifyDayGeoMatch(
        date: '2026-07-10',
        localOffset: zeroOffset,
        candidate: PhotoCandidate(capturedAt: sameDay, lat: 60.0, lon: 100.0),
      );
      expect(result, DayGeoMismatch.none);
    });
  });

  group('pairCandidatesWithThumbnails', () {
    test('a clear-cut single candidate/thumbnail pair is high confidence',
        () {
      final candidates = [
        PhotoCandidate(capturedAt: DateTime.utc(2026, 7, 10), pHash: 0x00),
      ];
      final result = pairCandidatesWithThumbnails(
        candidates: candidates,
        thumbnailPHashes: [0x03], // Hamming distance 2
      );

      expect(result.matches, hasLength(1));
      final match = result.matches.single;
      expect(match.candidateIndex, 0);
      expect(match.thumbnailIndex, 0);
      expect(match.hammingDistance, 2);
      expect(match.confidence, MatchConfidence.high);
      expect(result.unmatchedCandidateIndices, isEmpty);
      expect(result.unmatchedThumbnailIndices, isEmpty);
    });

    test('two candidates close in distance to the same thumbnail are '
        'flagged low-confidence rather than silently resolved', () {
      final candidates = [
        PhotoCandidate(capturedAt: DateTime.utc(2026, 7, 10), pHash: 0x01),
        PhotoCandidate(capturedAt: DateTime.utc(2026, 7, 10), pHash: 0x03),
      ];
      final result = pairCandidatesWithThumbnails(
        candidates: candidates,
        thumbnailPHashes: [0x00],
      );

      // Still produces a pairing (best available), but flags it.
      expect(result.matches, hasLength(1));
      final match = result.matches.single;
      expect(match.thumbnailIndex, 0);
      expect(match.candidateIndex, 0); // distance 1 beats distance 2
      expect(match.confidence, MatchConfidence.low);
      expect(result.unmatchedCandidateIndices, [1]);
    });

    test('a candidate with no pHash is left unmatched, not force-matched',
        () {
      final candidates = [
        PhotoCandidate(capturedAt: DateTime.utc(2026, 7, 10)), // no pHash
      ];
      final result = pairCandidatesWithThumbnails(
        candidates: candidates,
        thumbnailPHashes: [0x00],
      );

      expect(result.matches, isEmpty);
      expect(result.unmatchedCandidateIndices, [0]);
      expect(result.unmatchedThumbnailIndices, [0]);
    });

    test('more thumbnails than confidently-paired candidates leaves the '
        'rest unmatched', () {
      final candidates = [
        PhotoCandidate(capturedAt: DateTime.utc(2026, 7, 10), pHash: 0x00),
      ];
      final result = pairCandidatesWithThumbnails(
        candidates: candidates,
        thumbnailPHashes: [0x00, 0xFFFFFFFF], // second is far away
      );

      expect(result.matches, hasLength(1));
      expect(result.matches.single.thumbnailIndex, 0);
      expect(result.unmatchedThumbnailIndices, [1]);
    });
  });

  group('looksLikeSamePhoto', () {
    test('returns null when either side has no pHash', () {
      expect(looksLikeSamePhoto(null, 0x00), isNull);
      expect(looksLikeSamePhoto(0x00, null), isNull);
    });

    test('true when within the distance threshold', () {
      expect(looksLikeSamePhoto(0x00, 0x03), isTrue); // distance 2
    });

    test('false when past the distance threshold', () {
      expect(looksLikeSamePhoto(0x00, 0x7FFFFFFFFFFFFFFF), isFalse);
    });
  });

  group('hammingDistance', () {
    test('identical hashes have distance 0', () {
      expect(hammingDistance(0x1234, 0x1234), 0);
    });

    test('counts differing bits', () {
      expect(hammingDistance(0x00, 0x07), 3);
    });
  });
}
