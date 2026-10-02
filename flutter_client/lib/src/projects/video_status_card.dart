/// Small, passive status card for the trip-video job this client started or
/// resumed (docs/TRIP_VIDEO_PLAN.md, U7) — the video counterpart of
/// `poster_status_card.dart`, whose rules it follows: polling only drives
/// this card, never blocks, a long render is never turned into a failure by
/// a client timeout (only the server's `failed` counts), polling stops
/// silently after [VideoStatusNotifier.giveUpAfter], and the tracked job is
/// persisted so leaving the map and coming back resumes it.
///
/// Unlike the poster card this one can download: once done, the signed-in
/// user fetches the MP4 through the authenticated job route. That hands the
/// bytes to a browser download, so it is offered on the web only; the app
/// points to the emailed link instead, which opens `/video/{token}`.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:typed_data' show Uint8List;

import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/client.dart';
import '../api/video_api.dart';
import '../core/design_tokens.dart' show kShadow2;
import '../core/project_ref.dart';
import 'download_stub.dart' if (dart.library.js_interop) 'download_web.dart';

enum VideoCardState { hidden, rendering, done, failed, expired }

const _kVideoJobPrefKey = 'video_job_pending';

Future<void> _savePendingJob(ProjectRef ref, int jobId) async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(
      _kVideoJobPrefKey, jsonEncode({...ref.toJson(), 'jobId': jobId}));
}

Future<void> _clearPendingJob() async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.remove(_kVideoJobPrefKey);
}

Future<(ProjectRef, int)?> _readPendingJob() async {
  final prefs = await SharedPreferences.getInstance();
  final raw = prefs.getString(_kVideoJobPrefKey);
  if (raw == null || raw.isEmpty) return null;
  try {
    final decoded = jsonDecode(raw);
    if (decoded is Map) {
      final map = decoded.cast<String, dynamic>();
      final jobId = map['jobId'] as int?;
      if (jobId == null) return null;
      return (ProjectRef.fromJson(map), jobId);
    }
  } catch (_) {
    // Not valid JSON for this shape — treat as no persisted job.
  }
  return null;
}

/// Saves downloaded MP4 bytes; a browser download in production.
typedef VideoSaver = void Function(Uint8List bytes, String filename);

void _browserSave(Uint8List bytes, String filename) =>
    triggerBrowserDownload(bytes, 'video/mp4', filename);

class VideoStatusNotifier extends ChangeNotifier {
  /// Injectable for tests; production uses the shared [api] singleton.
  final ApiClient? client;
  final Duration pollInterval;

  /// A 90 s 1080p render takes minutes; past this the email is the word.
  final Duration giveUpAfter;
  final DateTime Function() _now;
  final VideoSaver _save;

  VideoStatusNotifier({
    this.client,
    this.pollInterval = const Duration(seconds: 5),
    this.giveUpAfter = const Duration(minutes: 30),
    DateTime Function()? now,
    VideoSaver? save,
  })  : _now = now ?? DateTime.now,
        _save = save ?? _browserSave;

  VideoCardState state = VideoCardState.hidden;
  String? stage;
  double progress = 0;
  bool downloading = false;
  String? downloadError;

  ProjectRef? _ref;
  int? _jobId;
  Timer? _timer;
  DateTime? _startedAt;

  /// Tracks [jobId], replacing any previous job (one card at a time).
  Future<void> start({required ProjectRef ref, required int jobId}) async {
    _timer?.cancel();
    _ref = ref;
    _jobId = jobId;
    _startedAt = _now();
    stage = null;
    progress = 0;
    downloadError = null;
    state = VideoCardState.rendering;
    notifyListeners();
    await _savePendingJob(ref, jobId);
    _schedulePoll();
  }

  /// One-shot resume check for [ref], as `PosterStatusNotifier.resume`:
  /// a still-running job reappears and polls, anything else is forgotten.
  Future<void> resume(ProjectRef ref) async {
    if (_jobId != null) return;
    final pending = await _readPendingJob();
    if (pending == null) return;
    final (pendingRef, jobId) = pending;
    if (pendingRef.name != ref.name || pendingRef.ownerId != ref.ownerId) return;

    try {
      final s = await fetchVideoJobStatus(ref: ref, jobId: jobId, client: client);
      if (s.isTerminal) {
        await _clearPendingJob();
        return;
      }
      _ref = ref;
      _jobId = jobId;
      _startedAt = _now();
      stage = s.stage;
      progress = s.progress;
      state = VideoCardState.rendering;
      notifyListeners();
      _schedulePoll();
    } catch (_) {
      await _clearPendingJob();
    }
  }

  void _schedulePoll() {
    _timer = Timer(pollInterval, _poll);
  }

  Future<void> _poll() async {
    final ref = _ref;
    final jobId = _jobId;
    final startedAt = _startedAt;
    if (ref == null || jobId == null || startedAt == null) return;
    if (_now().difference(startedAt) >= giveUpAfter) return;

    try {
      final s = await fetchVideoJobStatus(ref: ref, jobId: jobId, client: client);
      if (_jobId != jobId) return; // replaced or dismissed meanwhile
      stage = s.stage;
      progress = s.progress;
      if (s.isTerminal) {
        state = s.isDone
            ? VideoCardState.done
            : s.isFailed
                ? VideoCardState.failed
                : VideoCardState.expired;
        notifyListeners();
        await _clearPendingJob();
        return;
      }
      notifyListeners();
      _schedulePoll();
    } catch (_) {
      // Transient network hiccup — keep polling.
      _schedulePoll();
    }
  }

  /// Fetches the MP4 of the done job and saves it.
  Future<void> download() async {
    final ref = _ref;
    final jobId = _jobId;
    if (ref == null || jobId == null || state != VideoCardState.done) return;
    downloading = true;
    downloadError = null;
    notifyListeners();
    try {
      final res = await downloadVideoJob(ref: ref, jobId: jobId, client: client);
      _save(res.bodyBytes, '${ref.name}.mp4');
    } catch (_) {
      downloadError = 'Download failed — use the link in your email.';
    } finally {
      downloading = false;
      notifyListeners();
    }
  }

  Future<void> dismiss() async {
    _timer?.cancel();
    _timer = null;
    _ref = null;
    _jobId = null;
    stage = null;
    progress = 0;
    downloadError = null;
    state = VideoCardState.hidden;
    notifyListeners();
    await _clearPendingJob();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }
}

/// The card, styled like `PosterStatusCard`. Renders nothing when hidden.
class VideoStatusCard extends StatelessWidget {
  final VideoStatusNotifier notifier;

  /// Whether the done card offers an in-app download (see the library doc).
  final bool canDownload;

  const VideoStatusCard({
    super.key,
    required this.notifier,
    this.canDownload = kIsWeb,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: notifier,
      builder: (context, _) {
        final state = notifier.state;
        if (state == VideoCardState.hidden) return const SizedBox.shrink();
        final cs = Theme.of(context).colorScheme;
        final text = TextStyle(fontSize: 12.5, color: cs.onSurface);
        return Container(
          padding: const EdgeInsets.fromLTRB(14, 10, 10, 10),
          decoration: BoxDecoration(
            color: cs.surface.withValues(alpha: 0.95),
            borderRadius: BorderRadius.circular(12),
            boxShadow: kShadow2(Theme.of(context).brightness),
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _leading(cs),
                  const SizedBox(width: 8),
                  Text(_label, style: text),
                  if (state == VideoCardState.done && canDownload) ...[
                    const SizedBox(width: 6),
                    TextButton(
                      onPressed: notifier.downloading ? null : notifier.download,
                      child: notifier.downloading
                          ? const SizedBox(
                              width: 14,
                              height: 14,
                              child: CircularProgressIndicator(strokeWidth: 2))
                          : const Text('Download'),
                    ),
                  ],
                  if (state != VideoCardState.rendering) ...[
                    const SizedBox(width: 2),
                    IconButton(
                      tooltip: 'Dismiss',
                      icon: const Icon(Icons.close, size: 16),
                      padding: EdgeInsets.zero,
                      constraints:
                          const BoxConstraints(minWidth: 28, minHeight: 28),
                      onPressed: notifier.dismiss,
                    ),
                  ],
                ],
              ),
              if (notifier.downloadError != null) ...[
                const SizedBox(height: 4),
                Text(notifier.downloadError!,
                    style: TextStyle(fontSize: 11.5, color: cs.error)),
              ],
            ],
          ),
        );
      },
    );
  }

  Widget _leading(ColorScheme cs) {
    switch (notifier.state) {
      case VideoCardState.rendering:
        return SizedBox(
          width: 16,
          height: 16,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            // Indeterminate until the worker reports any progress.
            value: notifier.progress > 0 ? notifier.progress : null,
          ),
        );
      case VideoCardState.done:
        return Icon(Icons.check_circle, size: 18, color: cs.primary);
      case VideoCardState.failed:
        return Icon(Icons.error_outline, size: 18, color: cs.error);
      case VideoCardState.expired:
        return Icon(Icons.history, size: 18, color: cs.outline);
      case VideoCardState.hidden:
        return const SizedBox.shrink();
    }
  }

  String get _label {
    switch (notifier.state) {
      case VideoCardState.rendering:
        final pct = (notifier.progress * 100).round();
        return pct > 0 ? 'Rendering video… $pct%' : 'Rendering video…';
      case VideoCardState.done:
        return canDownload ? 'Video ready' : 'Video ready — check your email';
      case VideoCardState.failed:
        // Generic on purpose, as the poster card: errorMessage is internal.
        return "Video failed — it didn't count toward your quota";
      case VideoCardState.expired:
        return 'Video expired';
      case VideoCardState.hidden:
        return '';
    }
  }
}
