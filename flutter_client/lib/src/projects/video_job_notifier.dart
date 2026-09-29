/// Request flow for a trip video (docs/TRIP_VIDEO_PLAN.md, U7): plan, the
/// consent step for encrypted trips (D2), then the job.
///
/// [VideoRequestNotifier] is a plain [ChangeNotifier] so the flow is
/// testable without a widget tree; `video_config_dialog.dart` renders it and
/// asks for consent when [VideoRequestPhase.consentNeeded] comes up.
///
/// Consent geometry is built here, on the device, from the activities
/// `ProjectNotifier` already decrypted (see its `_revealActivities`), each
/// track `/meta` deferred fetched and decrypted here. It is held only in
/// memory for this one request and sent only after
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

/// Fetches one activity's stored geometry (the track editor's
/// `GET …/activities/{id}/track`, still encrypted); null when the trip has no
/// such activity. Throws when the request fails.
typedef TrackFetcher = Future<Map<String, dynamic>?> Function(int activityId);

/// The consent geometry for [ids]: each activity's decrypted track, or for
/// one that has no track its decrypted start and end as a 2-point line.
///
/// A track already merged into [activities] by the details fetch is used as
/// is. Otherwise — `/meta` defers polylines, so null there does not mean "no
/// track" — the activity's stored geometry is fetched with [fetchTrack] and
/// decrypted here; only when that confirms there is no track do the
/// endpoints stand in for it.
///
/// `missing` lists the ids this device could not decrypt (locked encryption,
/// a wrong key, or no geometry at all). `fetchFailed` is set when a fetch
/// failed; fetching stops there, since nothing is sent unless every
/// activity is there. [onProgress] is called with how many of [ids] are done.
Future<({Map<int, String> geometry, List<int> missing, bool fetchFailed})>
    buildConsentGeometry(
  List<int> ids,
  List<Map<String, dynamic>> activities, {
  required TrackFetcher fetchTrack,
  FieldRevealer? reveal,
  void Function(int done)? onProgress,
}) async {
  final doReveal = reveal ?? encryption.reveal;
  final byId = {for (final a in activities) a['id']?.toString(): a};
  final geometry = <int, String>{};
  final missing = <int>[];
  var done = 0;
  for (final id in ids) {
    var a = byId[id.toString()];
    if (a == null || _polylineOf(a) == null) {
      try {
        a = await fetchTrack(id);
      } catch (_) {
        return (geometry: geometry, missing: missing, fetchFailed: true);
      }
    }
    final line = a == null ? null : await _activityLine(a, doReveal);
    if (line == null) {
      missing.add(id);
    } else {
      geometry[id] = line;
    }
    onProgress?.call(++done);
  }
  return (geometry: geometry, missing: missing, fetchFailed: false);
}

bool _plain(String? v) =>
    v != null && v.isNotEmpty && !EncryptedField.isEnvelope(v);

String? _polylineOf(Map<String, dynamic> a) {
  final map = a['map'];
  final p = map is Map ? map['summary_polyline'] : null;
  return p is String && p.isNotEmpty ? p : null;
}

/// [a]'s track, decrypted; its endpoints when it has no track; null when
/// either can't be decrypted. A track that stays ciphertext is null, never
/// the endpoints: a straight line would silently stand in for a real track.
Future<String?> _activityLine(
    Map<String, dynamic> a, FieldRevealer reveal) async {
  final stored = _polylineOf(a);
  if (stored != null) {
    final polyline = await reveal(stored);
    if (!_plain(polyline)) return null;
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

  /// No broker, `video` worker or ffmpeg on the server (plan or 409 says
  /// so, or a 503). No consent is asked for and nothing is sent.
  unavailable,

  /// No video left this month (plan or 409 says so): [VideoRequestNotifier.quota]
  /// says how many were used. No consent is asked for and nothing is sent.
  noneLeft,

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

  /// Fetches an activity's stored track for consent; defaults to
  /// `GET …/activities/{id}/track` through [client].
  final TrackFetcher? fetchTrack;

  VideoRequestNotifier({
    required this.ref,
    required this.activities,
    this.client,
    this.reveal,
    this.fetchTrack,
  });

  VideoRequestPhase phase = VideoRequestPhase.loading;
  VideoPlan? plan;

  /// The quota a 409 carried, until a plan replaces it.
  VideoQuota? _consentQuota;

  /// The requester's quota, from the plan or else the 409.
  VideoQuota? get quota => plan?.quota ?? _consentQuota;

  int lengthS = 60;
  int? height;

  /// The dialog's camera choice: 'variable', 'overview' or 'fixed'.
  String cameraChoice = kVideoCameraDefault;

  /// With Fixed zoom: whether flights and long legs zoom out (`fixed`) or
  /// the whole video stays at one zoom (`fixed_strict`).
  bool fixedZoomOut = true;

  /// The `camera` field sent with the plan and the job (D1).
  String get camera => cameraChoice == 'fixed'
      ? (fixedZoomOut ? 'fixed' : 'fixed_strict')
      : cameraChoice;
  QuotaError? quotaError;
  String? errorMessage;
  int? jobId;

  /// Encrypted activities the server asked consent for.
  List<int> consentIds = const [];

  /// The consent geometry, once accepted. Memory only, this request only.
  Map<int, String>? _geometry;

  /// Whether the consent step interrupted [submit] rather than [loadPlan].
  bool _consentForSubmit = false;

  /// How many of [consentIds] have been fetched and decrypted, while
  /// [acceptConsent] runs; null otherwise.
  int? consentProgress;

  /// How many decrypted tracks this request will send.
  int get consentedCount => _geometry?.length ?? 0;

  bool get canSubmit =>
      phase == VideoRequestPhase.ready &&
      height != null &&
      quota?.exhausted != true;

  /// Once the dialog is gone nothing new is sent: a job created then would
  /// be charged and never shown.
  bool _disposed = false;

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  void _set(VideoRequestPhase p) {
    if (_disposed) return;
    phase = p;
    notifyListeners();
  }

  Future<Map<String, dynamic>?> _fetchTrack(int id) async {
    final f = fetchTrack;
    if (f != null) return f(id);
    final data =
        await (client ?? api).get(ref.path('/activities/$id/track'));
    return data as Map<String, dynamic>;
  }

  void setLength(int s) {
    lengthS = s;
    notifyListeners();
  }

  void setCamera(String choice) {
    cameraChoice = choice;
    notifyListeners();
  }

  void setFixedZoomOut(bool on) {
    fixedZoomOut = on;
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
          ref: ref,
          lengthS: lengthS,
          camera: camera,
          geometry: _geometry,
          client: client);
      plan = p;
      final allowed = p.resolutions;
      if (height == null || !allowed.contains(height)) {
        height = allowed.isEmpty ? null : allowed.last;
      }
      _set(!p.available
          ? VideoRequestPhase.unavailable
          : p.quota.exhausted
              ? VideoRequestPhase.noneLeft
              : VideoRequestPhase.ready);
    } catch (e) {
      _fail(e, forSubmit: false);
    }
  }

  Future<void> submit() async {
    final h = height;
    if (h == null || _disposed || quota?.exhausted == true) return;
    errorMessage = null;
    quotaError = null;
    _set(VideoRequestPhase.submitting);
    try {
      jobId = await createVideoJob(
          ref: ref,
          lengthS: lengthS,
          height: h,
          camera: camera,
          geometry: _geometry,
          client: client);
      _geometry = null;
      _set(VideoRequestPhase.started);
    } catch (e) {
      _fail(e, forSubmit: true);
    }
  }

  /// Fetches and decrypts the asked-for activities on this device and
  /// resumes what the 409 interrupted. Sends nothing when any of them can't
  /// be fetched or decrypted.
  Future<void> acceptConsent() async {
    consentProgress = 0;
    _set(VideoRequestPhase.loading);
    final built = await buildConsentGeometry(consentIds, activities(),
        fetchTrack: _fetchTrack, reveal: reveal, onProgress: (done) {
      consentProgress = done;
      if (!_disposed) notifyListeners();
    });
    consentProgress = null;
    if (_disposed) return;
    if (built.fetchFailed) {
      errorMessage = "Couldn't load this trip's encrypted tracks. Check your "
          'connection and try again.';
      _set(VideoRequestPhase.error);
      return;
    }
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
        // Never ask consent for a video that can't be made.
        if (consent.available == false) {
          _set(VideoRequestPhase.unavailable);
          return;
        }
        if (consent.quota?.exhausted == true) {
          _consentQuota = consent.quota;
          _set(VideoRequestPhase.noneLeft);
          return;
        }
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
