/// Job creation for async server-side A0 poster generation (issue #14, unit F).
///
/// Talks to the frozen contract in `api/poster.py`:
///   POST /api/projects/{name}/poster           -> {job_id}
///   POST /api/projects/{name}/poster/preview   -> PNG bytes (fast, no job)
///   GET  /api/projects/{name}/poster/{job_id}  -> {status, stage, error_message}
///
/// The render itself runs in a queued background worker and the user is
/// emailed a download link once it finishes (a parallel workstream), so the
/// client's job is only to kick the job off — this file itself does not
/// poll or block on the render. [fetchPosterJobStatus] below is a one-shot
/// status fetch re-added for the small ambient status card (issue #14, unit
/// G, `poster_status_card.dart`), which owns the actual poll loop; this is
/// deliberately not a resurrection of the old blocking-modal `PosterJobNotifier`
/// polling class that was deleted here — no state machine lives in this file.
library;

import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../core/project_ref.dart';
import '../crypto/encrypted_display.dart';

/// Converts a memory item (as found in `ProjectNotifier.items`) to the
/// `PosterMemoryIn` shape the poster API expects.
Map<String, dynamic> posterMemoryJson(Map<String, dynamic> memory) => {
      'id': memory['id'],
      'lat': memory['lat'],
      'lon': memory['lon'],
      'date': memory['date'],
      // Text this device could not decrypt is left off the poster (#466).
      'name': readableOrNull(memory['name'] as String?),
      'description': readableOrNull(memory['description'] as String?),
      'photo_uuids': (memory['photos'] as List?)?.cast<String>() ?? const [],
    };

/// A poster preview: the rendered PNG, plus a [warning] when the server could
/// render everything *except* the map imagery.
///
/// The distinction matters: a grey preview used to be indistinguishable from a
/// working one, so a broken `MAPBOX_TOKEN` looked like a design choice rather
/// than a fault the user could act on.
class PosterPreview {
  final Uint8List bytes;
  final String? warning;

  const PosterPreview({required this.bytes, this.warning});

  bool get hasWarning => warning != null && warning!.isNotEmpty;
}

/// Header the server uses to report a degraded (map-less) preview.
const kPosterWarningHeader = 'x-poster-warning';

/// Fetches a fast, low-resolution preview of the real poster — same request
/// shape as [createPosterJob], but synchronous (no job created). The preview
/// renders the same basemap, cards and placement as the full job, just
/// small, so what it shows is what the poster will look like.
Future<PosterPreview> fetchPosterPreview({
  required ProjectRef ref,
  required Map<String, double> bounds,
  required String orientation,
  required Map<String, dynamic> config,
  required List<Map<String, dynamic>> memories,
  // Matches `PosterRequest.paper_size` in `api/poster.py`; 'A0' preserves
  // existing behavior for any caller that doesn't pass this yet.
  String paperSize = 'A0',
  // Title/trip-summary card placement (poster_title_dialog.dart). Matches
  // `PosterRequest.title_position`/`title_text`/`title_scale`; the defaults
  // here preserve the pre-feature hardcoded top-left corner, the project's
  // own name, and the unscaled title size.
  Map<String, double> titlePosition = const {'x': 0.0, 'y': 0.0},
  String? titleText,
  double titleScale = 1.0,
  ApiClient? client,
  // The server caps its own basemap fetch at an 8s wall-clock budget
  // (poster_renderer._PREVIEW_BASEMAP_BUDGET_S) and degrades to a warning
  // past that, but card layout/measurement on top of it still needs more
  // headroom than the API client's 30s default gives it in practice.
  Duration timeout = const Duration(seconds: 20),
}) async {
  final res = await (client ?? api).postRaw(
    ref.path('/poster/preview'),
    {
      'bounds': bounds,
      'orientation': orientation,
      'paper_size': paperSize,
      'config': config,
      'memories': memories,
      'title_position': titlePosition,
      'title_text': titleText,
      'title_scale': titleScale,
    },
    timeout: timeout,
  );
  // Dart's http package lower-cases response header names.
  return PosterPreview(
    bytes: res.bodyBytes,
    warning: res.headers[kPosterWarningHeader],
  );
}

/// Kicks off a poster job and returns its id. [bounds] is
/// `{north, south, east, west}`; [config] matches `PosterConfigIn`'s field
/// names (see `PosterConfigOptions.toJson`).
///
/// Returns as soon as the server has queued the job — it does not wait for
/// (or poll for) the render to finish. A blocking, undismissable "wait up to
/// 120s" dialog built on top of this used to time out on real A0 renders that
/// legitimately take longer, reporting a false failure while the server kept
/// working (issue #14 feedback). The user is emailed a download link when
/// the render actually completes, so there is nothing left to wait for here;
/// callers should treat a thrown [ApiException] (or any other error) as a
/// failure to *create* the job, not a failure of the render itself.
Future<int> createPosterJob({
  required ProjectRef ref,
  required Map<String, double> bounds,
  required String orientation,
  required Map<String, dynamic> config,
  required List<Map<String, dynamic>> memories,
  // Matches `PosterRequest.paper_size` in `api/poster.py`; 'A0' preserves
  // existing behavior for any caller that doesn't pass this yet.
  String paperSize = 'A0',
  // Title/trip-summary card placement (poster_title_dialog.dart). Matches
  // `PosterRequest.title_position`/`title_text`/`title_scale`; the defaults
  // here preserve the pre-feature hardcoded top-left corner, the project's
  // own name, and the unscaled title size.
  Map<String, double> titlePosition = const {'x': 0.0, 'y': 0.0},
  String? titleText,
  double titleScale = 1.0,
  ApiClient? client,
}) async {
  final result = await (client ?? api).post(ref.path('/poster'), {
    'bounds': bounds,
    'orientation': orientation,
    'paper_size': paperSize,
    'config': config,
    'memories': memories,
    'title_position': titlePosition,
    'title_text': titleText,
    'title_scale': titleScale,
  }) as Map<String, dynamic>;
  return result['job_id'] as int;
}

/// A poster job's server-side status, as returned by
/// `GET /api/projects/{name}/poster/{job_id}` (`PosterJobStatusOut` in
/// `api/poster.py`).
class PosterJobStatus {
  /// 'pending' | 'running' | 'done' | 'failed'.
  final String status;

  /// Human-readable progress label (e.g. "Rendering basemap…"), or null.
  final String? stage;

  /// Set when [status] is 'failed'. Carries internal detail (e.g. a Mapbox
  /// error) — callers should not surface this verbatim to the user, the same
  /// restraint the failure email applies (see `_notify_poster_failed` in
  /// `src/poster/poster_job_runner.py`).
  final String? errorMessage;

  const PosterJobStatus({required this.status, this.stage, this.errorMessage});

  bool get isDone => status == 'done';
  bool get isFailed => status == 'failed';
  bool get isTerminal => isDone || isFailed;
}

/// Fetches a poster job's current status. A one-shot check, not a poll loop —
/// the small ambient status card (issue #14, unit G, `poster_status_card.dart`)
/// calls this on a timer and owns the actual polling/give-up logic; this
/// function only knows how to ask the server once.
Future<PosterJobStatus> fetchPosterJobStatus({
  required ProjectRef ref,
  required int jobId,
  ApiClient? client,
}) async {
  final result =
      await (client ?? api).get(ref.path('/poster/$jobId')) as Map<String, dynamic>;
  return PosterJobStatus(
    status: result['status'] as String,
    stage: result['stage'] as String?,
    errorMessage: result['error_message'] as String?,
  );
}
