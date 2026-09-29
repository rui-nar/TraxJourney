/// Client for the trip-video endpoints in `api/video.py`
/// (docs/TRIP_VIDEO_PLAN.md, U7):
///
///   POST /api/projects/{name}/video/plan              -> [VideoPlan]
///   POST /api/projects/{name}/video                   -> {job_id}
///   GET  /api/projects/{name}/video/{job_id}          -> [VideoJobStatus]
///   GET  /api/projects/{name}/video/{job_id}/download -> the MP4
///   GET  /api/video/{token}                           -> [VideoJobStatus]
///   GET  /api/video/{token}/download                  -> the MP4 (no session)
///
/// One-shot calls only, like `poster_job_notifier.dart`: the request flow
/// lives in `video_job_notifier.dart` and the poll loop in
/// `video_status_card.dart`. Errors come back as the [ApiException] the
/// server's status maps to; [VideoConsentRequired.fromApiException] reads a
/// 409 and `QuotaError.fromApiException` a 402.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import '../core/project_ref.dart';
import 'client.dart';

/// The lengths every plan may pick (D11).
const kVideoLengths = [30, 60, 90];

/// The server's camera modes (docs/VIDEO_CAMERA_QUALITY_PLAN.md, D1):
/// `variable` (default), `overview`, `fixed` and `fixed_strict`.
const kVideoCameraDefault = 'variable';

/// Planning and creating run the server's timeline over the whole trip, which
/// for a long trip takes longer than a plain JSON call.
const _kPlanTimeout = Duration(seconds: 60);

/// An MP4 of 90 s at 1080p is tens of MB.
const _kDownloadTimeout = Duration(minutes: 5);

/// `QuotaOut`: videos per UTC month. [limit] and [remaining] are null when
/// the requester has no limit.
class VideoQuota {
  final int? limit;
  final int used;
  final int? remaining;

  const VideoQuota({this.limit, this.used = 0, this.remaining});

  bool get unlimited => limit == null;

  /// No video left this month: nothing can be made, so nothing is asked for.
  bool get exhausted => remaining == 0;

  factory VideoQuota.fromJson(Map<String, dynamic> json) => VideoQuota(
        limit: (json['limit'] as num?)?.toInt(),
        used: (json['used'] as num?)?.toInt() ?? 0,
        remaining: (json['remaining'] as num?)?.toInt(),
      );
}

/// `VideoPlanOut`: what a video of the trip would be, for the dialog.
class VideoPlan {
  /// False when the server has no broker, no `video` worker or no ffmpeg to
  /// render with.
  final bool available;
  final int legs;

  /// Clip count per offered length, keyed by seconds.
  final Map<int, int> clipCounts;

  /// Trip items that can't be animated (no line, no date…).
  final int skipped;
  final VideoQuota quota;

  /// Heights the requester's plan allows, ascending.
  final List<int> resolutions;

  const VideoPlan({
    required this.available,
    required this.legs,
    required this.clipCounts,
    required this.skipped,
    required this.quota,
    required this.resolutions,
  });

  factory VideoPlan.fromJson(Map<String, dynamic> json) => VideoPlan(
        available: json['available'] == true,
        legs: (json['legs'] as num?)?.toInt() ?? 0,
        clipCounts: {
          for (final e in ((json['clip_counts'] as Map?) ?? const {}).entries)
            if (int.tryParse(e.key.toString()) != null)
              int.parse(e.key.toString()): (e.value as num).toInt(),
        },
        skipped: (json['skipped'] as List?)?.length ?? 0,
        quota: VideoQuota.fromJson(
            ((json['quota'] as Map?) ?? const {}).cast<String, dynamic>()),
        resolutions: [
          for (final h in (json['resolutions'] as List?) ?? const [])
            (h as num).toInt(),
        ],
      );
}

/// A 409 `consent_required`: the trip has encrypted activities the server
/// can't draw without their decrypted lines (D2). It also says whether a
/// video could be made at all, so consent is never asked for one that can't.
class VideoConsentRequired {
  final List<int> activityIds;
  final String message;

  /// The requester's quota; null when the server didn't send it.
  final VideoQuota? quota;

  /// Whether the server can render; null when it didn't say.
  final bool? available;

  const VideoConsentRequired(this.activityIds, this.message,
      {this.quota, this.available});

  /// Parse a 409 consent response, or null for any other failure.
  static VideoConsentRequired? fromApiException(ApiException e) {
    if (e.statusCode != 409) return null;
    try {
      final body = jsonDecode(e.body);
      final detail = body is Map ? body['detail'] : null;
      if (detail is! Map || detail['code'] != 'consent_required') return null;
      return VideoConsentRequired(
        [
          for (final id in (detail['consent_required'] as List?) ?? const [])
            (id as num).toInt(),
        ],
        detail['message'] as String? ?? '',
        quota: detail['quota'] is Map
            ? VideoQuota.fromJson(
                (detail['quota'] as Map).cast<String, dynamic>())
            : null,
        available: detail['available'] is bool
            ? detail['available'] as bool
            : null,
      );
    } catch (_) {
      return null;
    }
  }
}

/// `JobStatusOut`.
class VideoJobStatus {
  /// 'pending' | 'running' | 'done' | 'failed' | 'expired'.
  final String status;
  final String? stage;

  /// 0.0 – 1.0.
  final double progress;

  /// Set when failed. Internal detail — not for the user, like the poster's.
  final String? errorMessage;

  /// When a done video is deleted, seconds since the epoch.
  final double? expiresAt;

  const VideoJobStatus({
    required this.status,
    this.stage,
    this.progress = 0,
    this.errorMessage,
    this.expiresAt,
  });

  factory VideoJobStatus.fromJson(Map<String, dynamic> json) => VideoJobStatus(
        status: json['status'] as String,
        stage: json['stage'] as String?,
        progress: (json['progress'] as num?)?.toDouble() ?? 0,
        errorMessage: json['error_message'] as String?,
        expiresAt: (json['expires_at'] as num?)?.toDouble(),
      );

  bool get isDone => status == 'done';
  bool get isFailed => status == 'failed';
  bool get isExpired => status == 'expired';
  bool get isTerminal => isDone || isFailed || isExpired;
}

/// Consent geometry on the wire: JSON keys are strings, the server reads
/// them back as activity ids.
Map<String, String> _geometryJson(Map<int, String> geometry) =>
    {for (final e in geometry.entries) e.key.toString(): e.value};

/// Plans a video of [lengthS] seconds with the [camera] mode. [geometry] is
/// the consent geometry, sent only once the user agreed to it.
Future<VideoPlan> fetchVideoPlan({
  required ProjectRef ref,
  int lengthS = 60,
  String camera = kVideoCameraDefault,
  Map<int, String>? geometry,
  ApiClient? client,
}) async {
  final result = await (client ?? api).post(
    ref.path('/video/plan'),
    {
      'length_s': lengthS,
      'camera': camera,
      if (geometry != null) 'decrypted_geometry': _geometryJson(geometry),
    },
    timeout: _kPlanTimeout,
  ) as Map<String, dynamic>;
  return VideoPlan.fromJson(result);
}

/// Starts a render and returns its job id.
Future<int> createVideoJob({
  required ProjectRef ref,
  required int lengthS,
  required int height,
  String camera = kVideoCameraDefault,
  Map<int, String>? geometry,
  ApiClient? client,
}) async {
  final result = await (client ?? api).post(
    ref.path('/video'),
    {
      'length_s': lengthS,
      'height': height,
      'camera': camera,
      if (geometry != null) 'decrypted_geometry': _geometryJson(geometry),
    },
    timeout: _kPlanTimeout,
  ) as Map<String, dynamic>;
  return (result['job_id'] as num).toInt();
}

Future<VideoJobStatus> fetchVideoJobStatus({
  required ProjectRef ref,
  required int jobId,
  ApiClient? client,
}) async {
  final result = await (client ?? api).get(ref.path('/video/$jobId'))
      as Map<String, dynamic>;
  return VideoJobStatus.fromJson(result);
}

/// The MP4 of a done job, for a signed-in user.
Future<http.Response> downloadVideoJob({
  required ProjectRef ref,
  required int jobId,
  ApiClient? client,
}) =>
    (client ?? api)
        .getRaw(ref.path('/video/$jobId/download'), timeout: _kDownloadTimeout);

/// Status by the emailed download token; needs no session.
Future<VideoJobStatus> fetchVideoStatusByToken({
  required String token,
  ApiClient? client,
}) async {
  final result =
      await (client ?? api).get('/api/video/${Uri.encodeComponent(token)}')
          as Map<String, dynamic>;
  return VideoJobStatus.fromJson(result);
}

/// Absolute URL of the MP4 for [token]. The token is the credential, so the
/// browser or player can fetch it directly and stream it with range requests.
/// An empty base URL means same origin (the web build), resolved against the
/// page so the browser gets an absolute URL.
Uri videoDownloadUrl(String token, {ApiClient? client}) {
  final base = (client ?? api).baseUrl;
  final path = '/api/video/${Uri.encodeComponent(token)}/download';
  return base.isEmpty ? Uri.base.resolve(path) : Uri.parse('$base$path');
}
