/// Request flow for a trip video (docs/TRIP_VIDEO_PLAN.md, U7): plan, the
/// consent step for encrypted trips (D2), then the job.
///
/// [VideoRequestNotifier] is a plain [ChangeNotifier] so the flow is
/// testable without a widget tree; `video_config_dialog.dart` renders it and
/// asks for consent when [VideoRequestPhase.consentNeeded] comes up.
///
/// Consent geometry is built here, on the device, from the activities
/// `ProjectNotifier` already decrypted (see its `_revealActivities`). It is
/// held only in memory for this one request and sent only after
/// [VideoRequestNotifier.acceptConsent]; a declined request sends nothing.
library;

import 'dart:convert';

import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../api/video_api.dart';
import '../billing/billing_service.dart' show QuotaError;
import '../core/project_ref.dart';
import '../crypto/e2ee_crypto.dart' show EncryptedField;
import '../crypto/encryption.dart';
import '../map/polyline_decoder.dart';

/// Google-encodes [points] (`(lat, lon)`) at precision 5, the format the
/// server decodes consent geometry with.
///
/// Arithmetic only, no shifts or bitwise ops on accumulated values, for the
/// same reason as [decodePolyline]: on the web those run as 32-bit ops.
String encodePolyline(List<(double, double)> points) {
  final out = StringBuffer();
  void write(int delta) {
    var v = delta < 0 ? -delta * 2 - 1 : delta * 2;
    while (v >= 32) {
      out.writeCharCode(32 + v % 32 + 63);
      v = v ~/ 32;
    }
    out.writeCharCode(v + 63);
  }

  var lat = 0, lon = 0;
  for (final (pLat, pLon) in points) {
    final eLat = (pLat * 1e5).round();
    final eLon = (pLon * 1e5).round();
    write(eLat - lat);
    write(eLon - lon);
    lat = eLat;
    lon = eLon;
  }
  return out.toString();
}

/// Reveals one stored field; `encryption.reveal` in production.
typedef FieldRevealer = Future<String?> Function(String? value);

/// The consent geometry for [ids]: each activity's decrypted track, or for
/// one without a track its decrypted start and end as a 2-point line.
/// `missing` lists the ids this device could not decrypt (locked
/// encryption, a wrong key, or no geometry at all).
Future<({Map<int, String> geometry, List<int> missing})> buildConsentGeometry(
  List<int> ids,
  List<Map<String, dynamic>> activities, {
  FieldRevealer? reveal,
}) async {
  final doReveal = reveal ?? encryption.reveal;
  final byId = {for (final a in activities) a['id']?.toString(): a};
  final geometry = <int, String>{};
  final missing = <int>[];
  for (final id in ids) {
    final a = byId[id.toString()];
    final line = a == null ? null : await _activityLine(a, doReveal);
    if (line == null) {
      missing.add(id);
    } else {
      geometry[id] = line;
    }
  }
  return (geometry: geometry, missing: missing);
}

bool _plain(String? v) =>
    v != null && v.isNotEmpty && !EncryptedField.isEnvelope(v);

Future<String?> _activityLine(
    Map<String, dynamic> a, FieldRevealer reveal) async {
  final map = a['map'];
  final polyline = await reveal(
      map is Map ? map['summary_polyline'] as String? : null);
  if (_plain(polyline)) {
    try {
      if (decodePolyline(polyline!).length >= 2) return polyline;
    } catch (_) {
      // Not a polyline after all — fall back to the endpoints.
    }
  }
  final start = await _latLng(a['start_latlng'], a['start_latlng_enc'], reveal);
  final end = await _latLng(a['end_latlng'], a['end_latlng_enc'], reveal);
  if (start == null || end == null) return null;
  return encodePolyline([start, end]);
}

/// `[lat, lon]` from the revealed [value], else from decrypting [enc].
Future<(double, double)?> _latLng(
    dynamic value, dynamic enc, FieldRevealer reveal) async {
  var v = value;
  if (v is! List && enc is String) {
    final revealed = await reveal(enc);
    if (!_plain(revealed)) return null;
    try {
      v = jsonDecode(revealed!);
    } catch (_) {
      return null;
    }
  }
  if (v is! List || v.length < 2 || v[0] is! num || v[1] is! num) return null;
  return ((v[0] as num).toDouble(), (v[1] as num).toDouble());
}

enum VideoRequestPhase {
  loading,

  /// The plan is there and a video can be requested.
  ready,

  /// The server needs consent geometry; ask the user.
  consentNeeded,

  /// The user said no. Nothing was sent.
  declined,

  /// No broker or ffmpeg on the server (plan says so, or a 503).
  unavailable,

  /// A 402: [VideoRequestNotifier.quotaError] says which limit.
  quotaExceeded,
  error,
  submitting,

  /// The job exists: [VideoRequestNotifier.jobId].
  started,
}

class VideoRequestNotifier extends ChangeNotifier {
  final ProjectRef ref;

  /// The trip's activities as `ProjectNotifier` holds them (decrypted when
  /// encryption is unlocked).
  final List<Map<String, dynamic>> Function() activities;
  final ApiClient? client;
  final FieldRevealer? reveal;

  VideoRequestNotifier({
    required this.ref,
    required this.activities,
    this.client,
    this.reveal,
  });

  VideoRequestPhase phase = VideoRequestPhase.loading;
  VideoPlan? plan;
  int lengthS = 60;
  int? height;
  QuotaError? quotaError;
  String? errorMessage;
  int? jobId;

  /// Encrypted activities the server asked consent for.
  List<int> consentIds = const [];

  /// The consent geometry, once accepted. Memory only, this request only.
  Map<int, String>? _geometry;

  /// Whether the consent step interrupted [submit] rather than [loadPlan].
  bool _consentForSubmit = false;

  /// How many decrypted tracks this request will send.
  int get consentedCount => _geometry?.length ?? 0;

  bool get canSubmit => phase == VideoRequestPhase.ready && height != null;

  void _set(VideoRequestPhase p) {
    phase = p;
    notifyListeners();
  }

  void setLength(int s) {
    lengthS = s;
    notifyListeners();
  }

  void setHeight(int h) {
    height = h;
    // A refused resolution is fixed by picking another; a used-up month isn't.
    if (phase == VideoRequestPhase.quotaExceeded &&
        quotaError?.resource == 'video_height') {
      quotaError = null;
      phase = VideoRequestPhase.ready;
    }
    notifyListeners();
  }

  Future<void> loadPlan() async {
    errorMessage = null;
    _set(VideoRequestPhase.loading);
    try {
      final p = await fetchVideoPlan(
          ref: ref, lengthS: lengthS, geometry: _geometry, client: client);
      plan = p;
      final allowed = p.resolutions;
      if (height == null || !allowed.contains(height)) {
        height = allowed.isEmpty ? null : allowed.last;
      }
      _set(p.available ? VideoRequestPhase.ready : VideoRequestPhase.unavailable);
    } catch (e) {
      _fail(e, forSubmit: false);
    }
  }

  Future<void> submit() async {
    final h = height;
    if (h == null) return;
    errorMessage = null;
    quotaError = null;
    _set(VideoRequestPhase.submitting);
    try {
      jobId = await createVideoJob(
          ref: ref,
          lengthS: lengthS,
          height: h,
          geometry: _geometry,
          client: client);
      _geometry = null;
      _set(VideoRequestPhase.started);
    } catch (e) {
      _fail(e, forSubmit: true);
    }
  }

  /// Decrypts the asked-for activities on this device and resumes what the
  /// 409 interrupted. Sends nothing when any of them can't be decrypted.
  Future<void> acceptConsent() async {
    _set(VideoRequestPhase.loading);
    final built = await buildConsentGeometry(consentIds, activities(),
        reveal: reveal);
    if (built.missing.isNotEmpty) {
      errorMessage = "Some of this trip's encrypted activities can't be "
          'decrypted on this device. Unlock encryption and try again.';
      _set(VideoRequestPhase.error);
      return;
    }
    _geometry = {...?_geometry, ...built.geometry};
    if (_consentForSubmit) {
      await submit();
    } else {
      await loadPlan();
    }
  }

  void declineConsent() {
    _geometry = null;
    _set(VideoRequestPhase.declined);
  }

  void _fail(Object e, {required bool forSubmit}) {
    if (e is ApiException) {
      final consent = VideoConsentRequired.fromApiException(e);
      if (consent != null) {
        consentIds = consent.activityIds;
        _consentForSubmit = forSubmit;
        _set(VideoRequestPhase.consentNeeded);
        return;
      }
      final quota = QuotaError.fromApiException(e);
      if (quota != null) {
        quotaError = quota;
        _set(VideoRequestPhase.quotaExceeded);
        return;
      }
      if (e.statusCode == 503) {
        _set(VideoRequestPhase.unavailable);
        return;
      }
      errorMessage = e.statusCode == 422
          ? apiErrorDetail(e.body)
          : 'Could not ${forSubmit ? 'start' : 'plan'} the video. Please try again.';
    } else {
      errorMessage =
          'Could not ${forSubmit ? 'start' : 'plan'} the video. Please try again.';
    }
    _set(VideoRequestPhase.error);
  }
}
