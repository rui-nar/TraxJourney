import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:image/image.dart' as img;
import 'package:traxjourney_client/src/photos/immich_source.dart';
import 'package:traxjourney_client/src/photos/photo_match.dart';
import 'package:traxjourney_client/src/photos/photo_source.dart';

Uint8List _checkerboardPngBytes({int size = 32, int block = 4, bool invert = false}) {
  final image = img.Image(width: size, height: size);
  for (var y = 0; y < size; y++) {
    for (var x = 0; x < size; x++) {
      var on = ((x ~/ block) + (y ~/ block)).isEven;
      if (invert) on = !on;
      final v = on ? 255 : 0;
      image.setPixelRgb(x, y, v, v, v);
    }
  }
  return Uint8List.fromList(img.encodePng(image));
}

const _baseUrl = 'https://traxjourney.example.com';
const _authHeaders = {'Authorization': 'Bearer test-token'};

Map<String, dynamic> _rawCandidate({
  String id = 'asset-1',
  String takenAt = '2026-07-10T14:30:00Z',
  double? lat = 48.85,
  double? lon = 2.35,
  String thumbUrl = '/api/immich/assets/asset-1/thumb',
}) =>
    {
      'id': id,
      'taken_at': takenAt,
      'lat': lat,
      'lon': lon,
      'thumb_url': thumbUrl,
    };

void main() {
  group('fetchImmichCandidatesForDay', () {
    test('parses a well-formed search response', () async {
      final mock = MockClient((req) async {
        expect(req.method, 'POST');
        expect(req.url.toString(), '$_baseUrl/api/immich/search');
        expect(req.headers['Authorization'], 'Bearer test-token');
        final body = jsonDecode(req.body) as Map<String, dynamic>;
        expect(body['taken_after'], isNotNull);
        expect(body['taken_before'], isNotNull);
        return http.Response(
          jsonEncode({
            'candidates': [_rawCandidate()],
          }),
          200,
        );
      });

      final candidates = await fetchImmichCandidatesForDay(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        dayStartUtc: DateTime.utc(2026, 7, 10),
        dayEndUtc: DateTime.utc(2026, 7, 11),
        httpClient: mock,
      );

      expect(candidates, hasLength(1));
      expect(candidates.single.id, 'asset-1');
      expect(candidates.single.takenAt, DateTime.utc(2026, 7, 10, 14, 30));
      expect(candidates.single.lat, 48.85);
      expect(candidates.single.lon, 2.35);
      expect(candidates.single.thumbUrl, '/api/immich/assets/asset-1/thumb');
    });

    test('a non-2xx response returns an empty list, not a throw', () async {
      final mock = MockClient((req) async => http.Response('boom', 500));

      final candidates = await fetchImmichCandidatesForDay(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        dayStartUtc: DateTime.utc(2026, 7, 10),
        dayEndUtc: DateTime.utc(2026, 7, 11),
        httpClient: mock,
      );

      expect(candidates, isEmpty);
    });

    test('malformed JSON returns an empty list, not a throw', () async {
      final mock = MockClient((req) async => http.Response('not json', 200));

      final candidates = await fetchImmichCandidatesForDay(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        dayStartUtc: DateTime.utc(2026, 7, 10),
        dayEndUtc: DateTime.utc(2026, 7, 11),
        httpClient: mock,
      );

      expect(candidates, isEmpty);
    });

    test('a response missing "candidates" returns an empty list', () async {
      final mock = MockClient((req) async => http.Response(jsonEncode({'ok': true}), 200));

      final candidates = await fetchImmichCandidatesForDay(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        dayStartUtc: DateTime.utc(2026, 7, 10),
        dayEndUtc: DateTime.utc(2026, 7, 11),
        httpClient: mock,
      );

      expect(candidates, isEmpty);
    });

    test('a malformed individual candidate is dropped, not the whole batch', () async {
      final mock = MockClient((req) async => http.Response(
            jsonEncode({
              'candidates': [
                _rawCandidate(id: 'good'),
                {'id': 'missing-taken-at', 'thumb_url': '/x'},
                {'taken_at': 'not-a-date', 'id': 'bad-date', 'thumb_url': '/x'},
              ],
            }),
            200,
          ));

      final candidates = await fetchImmichCandidatesForDay(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        dayStartUtc: DateTime.utc(2026, 7, 10),
        dayEndUtc: DateTime.utc(2026, 7, 11),
        httpClient: mock,
      );

      expect(candidates, hasLength(1));
      expect(candidates.single.id, 'good');
    });

    test('a network failure returns an empty list, not a throw', () async {
      final mock = MockClient((req) async => throw Exception('connection refused'));

      final candidates = await fetchImmichCandidatesForDay(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        dayStartUtc: DateTime.utc(2026, 7, 10),
        dayEndUtc: DateTime.utc(2026, 7, 11),
        httpClient: mock,
      );

      expect(candidates, isEmpty);
    });
  });

  group('filterImmichCandidatesForDay', () {
    // Close to farAway's memory location (48.0, 2.0) below, unlike farAway
    // itself -- the geo test needs one candidate inside tolerance and one
    // outside.
    final onDay = ImmichCandidate(
      id: 'on-day',
      takenAt: DateTime.utc(2026, 7, 10, 12, 0),
      lat: 48.01,
      lon: 2.01,
      thumbUrl: '/thumb/on-day',
    );
    final otherDay = ImmichCandidate(
      id: 'other-day',
      takenAt: DateTime.utc(2026, 6, 1, 12, 0),
      lat: 48.01,
      lon: 2.01,
      thumbUrl: '/thumb/other-day',
    );
    final farAway = ImmichCandidate(
      id: 'far-away',
      takenAt: DateTime.utc(2026, 7, 10, 12, 0),
      lat: 49.0, // ~111km from 48.0 -- outside the 50km default tolerance
      lon: 2.0,
      thumbUrl: '/thumb/far-away',
    );

    test('excludes candidates outside the memory day', () {
      final result = filterImmichCandidatesForDay(
        date: '2026-07-10',
        localOffset: Duration.zero,
        candidates: [onDay, otherDay],
      );

      expect(result.map((c) => c.id), ['on-day']);
    });

    test('excludes candidates outside the geo tolerance when the memory has a location', () {
      final result = filterImmichCandidatesForDay(
        date: '2026-07-10',
        localOffset: Duration.zero,
        candidates: [onDay, farAway],
        memoryLat: 48.0,
        memoryLon: 2.0,
      );

      expect(result.map((c) => c.id), ['on-day']);
    });

    test('keeps day-matched candidates when the memory has no location', () {
      final result = filterImmichCandidatesForDay(
        date: '2026-07-10',
        localOffset: Duration.zero,
        candidates: [onDay, farAway],
      );

      expect(result.map((c) => c.id), containsAll(['on-day', 'far-away']));
    });
  });

  group('downloadImmichCandidate', () {
    final candidate = ImmichCandidate(
      id: 'asset-1',
      takenAt: DateTime.utc(2026, 7, 10, 14, 30),
      lat: 48.85,
      lon: 2.35,
      thumbUrl: '/thumb/asset-1',
    );

    test('builds a PickedPhoto with a computed pHash from the downloaded bytes', () async {
      final bytes = _checkerboardPngBytes();
      final mock = MockClient((req) async {
        expect(req.method, 'GET');
        expect(req.url.toString(), '$_baseUrl/api/immich/assets/asset-1/original');
        expect(req.headers['Authorization'], 'Bearer test-token');
        return http.Response.bytes(bytes, 200);
      });

      final photo = await downloadImmichCandidate(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        candidate: candidate,
        httpClient: mock,
      );

      expect(photo.bytes, bytes);
      expect(photo.candidate, isNotNull);
      expect(photo.candidate!.capturedAt, candidate.takenAt);
      expect(photo.candidate!.lat, 48.85);
      expect(photo.candidate!.lon, 2.35);
      expect(photo.candidate!.pHash, isNotNull);
    });

    test('a candidate downloaded via Immich pHash-matches its own thumbnail', () async {
      final bytes = _checkerboardPngBytes();
      final thumbnailHash = computeAverageHash(bytes);
      final mock = MockClient((req) async => http.Response.bytes(bytes, 200));

      final photo = await downloadImmichCandidate(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        candidate: candidate,
        httpClient: mock,
      );

      expect(looksLikeSamePhoto(photo.candidate?.pHash, thumbnailHash), isTrue);
    });

    test('a visually different candidate does not pHash-match the thumbnail', () async {
      final downloadedBytes = _checkerboardPngBytes();
      final thumbnailHash = computeAverageHash(_checkerboardPngBytes(invert: true));
      final mock = MockClient((req) async => http.Response.bytes(downloadedBytes, 200));

      final photo = await downloadImmichCandidate(
        baseUrl: _baseUrl,
        authHeaders: _authHeaders,
        candidate: candidate,
        httpClient: mock,
      );

      expect(looksLikeSamePhoto(photo.candidate?.pHash, thumbnailHash), isFalse);
    });

    test('a non-2xx response throws instead of returning an unusable photo', () async {
      final mock = MockClient((req) async => http.Response('not found', 404));

      await expectLater(
        downloadImmichCandidate(
          baseUrl: _baseUrl,
          authHeaders: _authHeaders,
          candidate: candidate,
          httpClient: mock,
        ),
        throwsA(isA<Exception>()),
      );
    });
  });
}
