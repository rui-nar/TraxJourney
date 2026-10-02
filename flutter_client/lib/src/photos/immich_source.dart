/// Immich candidate source for the photo-upgrade feature (issue #33).
///
/// Mirrors `photo_source.dart`'s device-file-picker source, but candidates
/// come from a self-hosted Immich library over the network instead of the
/// system file picker. Two-step by design: [fetchImmichCandidatesForDay]
/// pulls cheap metadata (capture time, GPS, a thumbnail URL) for a whole day
/// in one call, then [filterImmichCandidatesForDay] narrows that down to the
/// memory's day/place using the same rules `photo_match.dart` applies to
/// on-device candidates — all before a single full-resolution byte is
/// downloaded. Only [downloadImmichCandidate], called for the one candidate
/// a caller actually wants to compare or apply, pulls the full original and
/// computes its pHash, so reviewing a day's worth of candidates never pays
/// for full-res downloads it doesn't need.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import 'perceptual_hash.dart';
import 'photo_match.dart';
import 'photo_source.dart' show PickedPhoto;

/// A candidate photo from an Immich library. Unlike `photo_source.dart`'s
/// [PickedPhoto], capture time/GPS come straight from Immich's search
/// response — no EXIF re-extraction needed — and [pHash] isn't known until
/// [downloadImmichCandidate] pulls the full bytes.
class ImmichCandidate {
  final String id;
  final DateTime takenAt;
  final double? lat;
  final double? lon;

  /// Same-origin relative path (e.g. `/api/immich/assets/{id}/thumb`),
  /// already suitable for `Image.network` the same way `photoThumbUrl` is.
  final String thumbUrl;

  const ImmichCandidate({
    required this.id,
    required this.takenAt,
    this.lat,
    this.lon,
    required this.thumbUrl,
  });
}

/// Queries `/api/immich/search` for every asset captured within
/// [dayStartUtc, dayEndUtc) — callers derive that window the same way
/// `photo_match.dart`'s day bounds do (local midnight minus the trip's UTC
/// offset). Only metadata comes back; no image bytes.
///
/// Never throws: Immich is an optional enrichment source for this dialog, so
/// a network failure, a non-2xx response, or a malformed body all resolve to
/// an empty list rather than surfacing an exception to the UI layer.
Future<List<ImmichCandidate>> fetchImmichCandidatesForDay({
  required String baseUrl,
  required Map<String, String> authHeaders,
  required DateTime dayStartUtc,
  required DateTime dayEndUtc,
  // Injectable so tests can supply a MockClient (mirrors ApiClient's style).
  http.Client? httpClient,
}) async {
  final client = httpClient ?? http.Client();
  try {
    final res = await client.post(
      Uri.parse('$baseUrl/api/immich/search'),
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: jsonEncode({
        'taken_after': dayStartUtc.toUtc().toIso8601String(),
        'taken_before': dayEndUtc.toUtc().toIso8601String(),
      }),
    );
    if (res.statusCode < 200 || res.statusCode >= 300) return [];

    final decoded = jsonDecode(res.body);
    if (decoded is! Map || decoded['candidates'] is! List) return [];

    final candidates = <ImmichCandidate>[];
    for (final raw in decoded['candidates'] as List) {
      final candidate = _parseCandidate(raw);
      if (candidate != null) candidates.add(candidate);
    }
    return candidates;
  } catch (_) {
    return [];
  }
}

/// Parses one `candidates[]` entry, or null if it's missing a required
/// field or has an unparseable timestamp — a single bad entry is dropped
/// rather than failing the whole search.
ImmichCandidate? _parseCandidate(dynamic raw) {
  if (raw is! Map) return null;
  final id = raw['id'];
  final takenAtRaw = raw['taken_at'];
  final thumbUrl = raw['thumb_url'];
  if (id is! String || takenAtRaw is! String || thumbUrl is! String) {
    return null;
  }
  final takenAt = DateTime.tryParse(takenAtRaw);
  if (takenAt == null) return null;

  return ImmichCandidate(
    id: id,
    takenAt: takenAt,
    lat: (raw['lat'] as num?)?.toDouble(),
    lon: (raw['lon'] as num?)?.toDouble(),
    thumbUrl: thumbUrl,
  );
}

/// Narrows [candidates] to a memory's local [date] (and, when the memory has
/// a location, within [geoToleranceKm]) — reuses `photo_match.dart`'s
/// [classifyDayGeoMatch] (treating each [ImmichCandidate] as a
/// [PhotoCandidate]) so the day/geo rules can never silently drift between
/// the device-picker and Immich sources. Pure and cheap: candidates carry no
/// pHash yet, so this never downloads anything — it's the filter to run
/// before deciding which candidates are even worth a full-res download.
List<ImmichCandidate> filterImmichCandidatesForDay({
  required String date,
  required Duration localOffset,
  required List<ImmichCandidate> candidates,
  double? memoryLat,
  double? memoryLon,
  double geoToleranceKm = kDefaultGeoToleranceKm,
}) {
  return candidates.where((c) {
    final mismatch = classifyDayGeoMatch(
      date: date,
      localOffset: localOffset,
      candidate: PhotoCandidate(capturedAt: c.takenAt, lat: c.lat, lon: c.lon),
      memoryLat: memoryLat,
      memoryLon: memoryLon,
      geoToleranceKm: geoToleranceKm,
    );
    return mismatch == DayGeoMismatch.none;
  }).toList();
}

/// Downloads [candidate]'s full-resolution original and builds a
/// [PickedPhoto] from it, computing its pHash (reusing `photo_source.dart`'s
/// [computeAverageHash]) so it can be compared against a memory's thumbnail
/// via `photo_match.dart`'s [looksLikeSamePhoto]. Unlike
/// `photo_source.dart`'s device-picker path, no EXIF extraction happens
/// here — Immich's search response already supplied capture time/GPS, so
/// [candidate]'s own fields become the resulting [PhotoCandidate] directly.
///
/// Only ever called for the one candidate a caller is actually reviewing —
/// call [fetchImmichCandidatesForDay] + [filterImmichCandidatesForDay] first
/// to avoid pulling full-res bytes for every candidate in a day.
///
/// Throws [Exception] on a non-2xx response or a network failure — unlike
/// the search above, a caller here made an explicit choice to download one
/// specific photo, so a failure should surface rather than silently produce
/// an unusable photo.
Future<PickedPhoto> downloadImmichCandidate({
  required String baseUrl,
  required Map<String, String> authHeaders,
  required ImmichCandidate candidate,
  http.Client? httpClient,
}) async {
  final client = httpClient ?? http.Client();
  final res = await client.get(
    Uri.parse('$baseUrl/api/immich/assets/${candidate.id}/original'),
    headers: authHeaders,
  );
  if (res.statusCode < 200 || res.statusCode >= 300) {
    throw Exception(
        'Failed to download Immich photo ${candidate.id}: HTTP ${res.statusCode}');
  }

  final bytes = res.bodyBytes;
  final pHash = computeAverageHash(bytes);
  final photoCandidate = PhotoCandidate(
    capturedAt: candidate.takenAt,
    lat: candidate.lat,
    lon: candidate.lon,
    pHash: pHash,
  );

  return PickedPhoto(
    bytes: bytes,
    filename: 'immich-${candidate.id}.jpg',
    candidate: photoCandidate,
  );
}
