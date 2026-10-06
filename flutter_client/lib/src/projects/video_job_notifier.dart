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
/// A preview (docs/VIDEO_PREVIEW_PLAN.md, D8) re-sends the same geometry, so
/// one consent covers the previews and the video of a dialog session.
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
import '../track_metrics/polyline_encoder.dart';

// The encoder now lives in track_metrics/; callers of this module still get it.
export '../track_metrics/polyline_encoder.dart' show encodePolyline;

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

/// Where a preview is (docs/VIDEO_PREVIEW_PLAN.md, U4). Independent of
/// [VideoRequestPhase]: the video can be created while a preview renders.
enum VideoPreviewPhase {
  /// None asked for, or the last one expired.
  idle,

  /// The preview is being requested.
  requesting,

  /// Queued, waiting for a worker.
  pending,

  /// A worker is rendering it.
  running,

  /// [VideoRequestNotifier.previewBytes] holds it.
  done,

  /// The server failed it.
  failed,

  /// Still waiting for a worker after [kPreviewPendingDeadline].
  busy,

  /// Still rendering after [kPreviewRunningDeadline].
  tooLong,

  /// A 429: [VideoRequestNotifier.previewRetryAfterS] says for how long.
  rateLimited,

  /// A 503: no broker or no worker to render it.
  unavailable,

  /// Anything else: [VideoRequestNotifier.previewError] says what.
  error,
}

/// How often a preview's status is polled.
const kPreviewPollInterval = Duration(seconds: 2);

/// Previews share a queue with other jobs (D5), so the wait for a worker can
/// be minutes; past this the dialog stops waiting.
const kPreviewPendingDeadline = Duration(minutes: 10);

/// Past the server's 300 s job timeout, so a slow render that succeeds is
/// never abandoned.
const kPreviewRunningDeadline = Duration(minutes: 6);

/// The settings a preview was made for; any change makes it out of date.
typedef VideoPreviewSettings = ({int lengthS, String camera, int height});

/// What the consent step interrupted, to resume once accepted.
enum _ConsentFor { plan, submit, preview }

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

  /// The preview deadlines' clock and poll wait; injectable for tests.
  final DateTime Function() _now;
  final Future<void> Function(Duration) _wait;

  VideoRequestNotifier({
    required this.ref,
    required this.activities,
    this.client,
    this.reveal,
    this.fetchTrack,
    DateTime Function()? now,
    Future<void> Function(Duration)? wait,
  })  : _now = now ?? DateTime.now,
        _wait = wait ?? ((d) => Future<void>.delayed(d));

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

  /// What the consent step interrupted.
  _ConsentFor _consentFor = _ConsentFor.plan;

  /// With [_ConsentFor.preview]: the phase the preview's 409 found, which
  /// accepting or declining puts back (a used-up month stays used up).
  VideoRequestPhase _phaseBeforeConsent = VideoRequestPhase.ready;

  /// How many of [consentIds] have been fetched and decrypted, while
  /// [acceptConsent] runs; null otherwise.
  int? consentProgress;

  /// How many decrypted tracks this request will send.
  int get consentedCount => _geometry?.length ?? 0;

  VideoPreviewPhase previewPhase = VideoPreviewPhase.idle;

  /// The last preview made, animated WebP; kept while another is made.
  Uint8List? previewBytes;

  /// The settings [previewBytes] was made for.
  VideoPreviewSettings? previewSettings;

  /// With [VideoPreviewPhase.rateLimited]: seconds until the next preview.
  int? previewRetryAfterS;

  /// With [VideoPreviewPhase.error].
  String? previewError;

  /// Bumped by each preview: an older poll loop sees it and stops.
  int _previewGen = 0;

  VideoPreviewSettings? get _settings {
    final h = height;
    return h == null ? null : (lengthS: lengthS, camera: camera, height: h);
  }

  /// A preview is shown but the length, camera or resolution changed since.
  bool get previewOutOfDate =>
      previewBytes != null && previewSettings != _settings;

  bool get previewInFlight =>
      previewPhase == VideoPreviewPhase.requesting ||
      previewPhase == VideoPreviewPhase.pending ||
      previewPhase == VideoPreviewPhase.running;

  /// Previews are free (D3) and render on the default queue, so neither the
  /// monthly quota nor the `video` worker being down gates them.
  bool get canPreview =>
      _previewable(phase) && height != null && !previewInFlight;

  static bool _previewable(VideoRequestPhase p) =>
      p == VideoRequestPhase.ready ||
      p == VideoRequestPhase.quotaExceeded ||
      p == VideoRequestPhase.noneLeft ||
      p == VideoRequestPhase.unavailable;

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

  /// Fetches the plan for the current settings and any consented geometry,
  /// keeps it and a resolution it allows. Throws what the request throws.
  Future<VideoPlan> _fetchPlan() async {
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
    return p;
  }

  Future<void> loadPlan() async {
    errorMessage = null;
    _set(VideoRequestPhase.loading);
    try {
      final p = await _fetchPlan();
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
    switch (_consentFor) {
      case _ConsentFor.submit:
        await submit();
      case _ConsentFor.plan:
        await loadPlan();
      case _ConsentFor.preview:
        // An encrypted trip's plan is empty without its geometry while no
        // video can be made; with it the clip counts are real. The phase
        // stays what the preview's 409 found, and a failed reload keeps the
        // previous plan: the preview goes ahead either way.
        try {
          await _fetchPlan();
        } catch (_) {
          // Keep the plan there was.
        }
        if (_disposed) return;
        _set(_phaseBeforeConsent);
        await preview();
    }
  }

  /// Declining ends the request, except for a preview: the dialog goes back
  /// to where it was and no preview is made.
  void declineConsent() {
    if (_consentFor == _ConsentFor.preview) {
      _setPreview(VideoPreviewPhase.idle);
      _set(_phaseBeforeConsent);
      return;
    }
    _geometry = null;
    _set(VideoRequestPhase.declined);
  }

  void _setPreview(VideoPreviewPhase p) {
    if (_disposed) return;
    previewPhase = p;
    notifyListeners();
  }

  /// Requests a preview of the current settings with any consented geometry,
  /// polls it until it is done or a deadline passes, then fetches it. A 409
  /// asks for consent ([VideoRequestPhase.consentNeeded]); [acceptConsent]
  /// then asks again.
  Future<void> preview() async {
    final settings = _settings;
    if (settings == null || _disposed || !canPreview) return;
    final gen = ++_previewGen;
    bool stale() => _disposed || gen != _previewGen;
    previewError = null;
    previewRetryAfterS = null;
    _setPreview(VideoPreviewPhase.requesting);
    final int id;
    try {
      id = await createVideoPreview(
          ref: ref,
          lengthS: settings.lengthS,
          height: settings.height,
          camera: settings.camera,
          geometry: _geometry,
          client: client);
    } catch (e) {
      if (!stale()) _previewFailed(e);
      return;
    }
    if (stale()) return;
    _setPreview(VideoPreviewPhase.pending);
    final queuedAt = _now();
    DateTime? runningSince;
    while (true) {
      await _wait(kPreviewPollInterval);
      if (stale()) return;
      VideoJobStatus? s;
      try {
        s = await fetchVideoPreviewStatus(ref: ref, jobId: id, client: client);
      } on ApiException catch (e) {
        // A 4xx won't change by asking again; anything else may be a blip.
        if (e.statusCode >= 400 && e.statusCode < 500) {
          if (!stale()) _previewFailed(e);
          return;
        }
      } catch (_) {
        // Transient network hiccup — keep polling until a deadline.
      }
      if (stale()) return;
      if (s != null) {
        if (s.isFailed) return _setPreview(VideoPreviewPhase.failed);
        if (s.isExpired) return _setPreview(VideoPreviewPhase.idle);
        if (s.isDone) return _fetchPreview(id, settings, gen);
        if (s.status == 'running') runningSince ??= _now();
      }
      final t = _now();
      final since = runningSince;
      if (since != null) {
        if (t.difference(since) > kPreviewRunningDeadline) {
          return _setPreview(VideoPreviewPhase.tooLong);
        }
        if (previewPhase != VideoPreviewPhase.running) {
          _setPreview(VideoPreviewPhase.running);
        }
      } else if (t.difference(queuedAt) > kPreviewPendingDeadline) {
        return _setPreview(VideoPreviewPhase.busy);
      }
    }
  }

  Future<void> _fetchPreview(
      int id, VideoPreviewSettings settings, int gen) async {
    final Uint8List bytes;
    try {
      bytes = await fetchVideoPreviewBytes(ref: ref, jobId: id, client: client);
    } catch (_) {
      if (_disposed || gen != _previewGen) return;
      previewError = "Couldn't load the preview. Please try again.";
      return _setPreview(VideoPreviewPhase.error);
    }
    if (_disposed || gen != _previewGen) return;
    previewBytes = bytes;
    previewSettings = settings;
    _setPreview(VideoPreviewPhase.done);
  }

  /// A refused preview request. Its 409 carries the monthly video quota and
  /// `available: true` whatever they are (previews are free and render on
  /// another worker), so neither is read here.
  void _previewFailed(Object e) {
    if (e is ApiException) {
      final consent = VideoConsentRequired.fromApiException(e);
      if (consent != null) {
        // The video's own request moved on meanwhile (it is being created,
        // or asks consent itself): that step owns the phase, and it will ask
        // for the same consent if it needs it.
        if (!_previewable(phase)) return _setPreview(VideoPreviewPhase.idle);
        consentIds = consent.activityIds;
        _consentFor = _ConsentFor.preview;
        _phaseBeforeConsent = phase;
        previewPhase = VideoPreviewPhase.idle;
        _set(VideoRequestPhase.consentNeeded);
        return;
      }
      final limited = VideoPreviewRateLimited.fromApiException(e);
      if (limited != null) {
        previewRetryAfterS = limited.retryAfterS;
        return _setPreview(VideoPreviewPhase.rateLimited);
      }
      if (e.statusCode == 503) {
        return _setPreview(VideoPreviewPhase.unavailable);
      }
      previewError = e.statusCode == 422
          ? apiErrorDetail(e.body)
          : 'Could not make the preview. Please try again.';
    } else {
      previewError = 'Could not make the preview. Please try again.';
    }
    _setPreview(VideoPreviewPhase.error);
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
        _consentFor = forSubmit ? _ConsentFor.submit : _ConsentFor.plan;
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
