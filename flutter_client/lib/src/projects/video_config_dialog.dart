/// Dialog for requesting a trip video (docs/TRIP_VIDEO_PLAN.md, U7): pick a
/// length (30/60/90 s, D11), a resolution the plan allows (D10) and a camera
/// (docs/VIDEO_CAMERA_QUALITY_PLAN.md, D1), see the monthly quota (D3), then
/// create. Renders a [VideoRequestNotifier] and runs
/// its consent step (D2) through [showVideoConsentDialog]. A Preview button
/// shows a low-resolution animation of the chosen settings first
/// (docs/VIDEO_PREVIEW_PLAN.md, U4).
///
/// Pops itself when the job has started (handing the id to [onStarted]) or
/// when the user declines consent. It can't be closed while the job is being
/// created: the server would make (and charge) a job nobody is shown.
library;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/video_api.dart';
import '../billing/upgrade_sheet.dart';
import '../core/design_tokens.dart' show kWarning, kWarningDark;
import '../core/project_ref.dart';
import 'video_consent_dialog.dart';
import 'video_job_notifier.dart';
import 'video_preview_image.dart';

class VideoConfigDialog extends StatefulWidget {
  final ProjectRef projectRef;
  final List<Map<String, dynamic>> Function() activities;
  final void Function(int jobId) onStarted;

  /// Injectable for tests; production uses the shared [api] singleton.
  final ApiClient? client;
  final FieldRevealer? reveal;
  final TrackFetcher? fetchTrack;

  /// The preview's clock and poll wait; injectable for tests.
  final DateTime Function()? now;
  final Future<void> Function(Duration)? wait;

  const VideoConfigDialog({
    super.key,
    required this.projectRef,
    required this.activities,
    required this.onStarted,
    this.client,
    this.reveal,
    this.fetchTrack,
    this.now,
    this.wait,
  });

  @override
  State<VideoConfigDialog> createState() => _VideoConfigDialogState();
}

class _VideoConfigDialogState extends State<VideoConfigDialog> {
  late final VideoRequestNotifier _n = VideoRequestNotifier(
    ref: widget.projectRef,
    activities: widget.activities,
    client: widget.client,
    reveal: widget.reveal,
    fetchTrack: widget.fetchTrack,
    now: widget.now,
    wait: widget.wait,
  );

  /// Set once this dialog pops itself. A preview still polling then must not
  /// act on the phase: the dialog stays mounted through its exit animation,
  /// and a second pop would close the screen under it.
  bool _closed = false;

  /// Whether the consent dialog is up.
  bool _asking = false;

  @override
  void initState() {
    super.initState();
    _run(_n.loadPlan);
  }

  @override
  void dispose() {
    _n.dispose();
    super.dispose();
  }

  /// Runs [step], then whatever the phase it ends in asks of the UI.
  Future<void> _run(Future<void> Function() step) async {
    await step();
    if (!mounted || _closed) return;
    switch (_n.phase) {
      case VideoRequestPhase.consentNeeded:
        // A preview can end while another step's consent is being asked.
        if (_asking) return;
        _asking = true;
        final ok = await showVideoConsentDialog(context,
            activityCount: _n.consentIds.length);
        _asking = false;
        if (!mounted || _closed) return;
        if (ok) {
          await _run(_n.acceptConsent);
        } else {
          _n.declineConsent();
          // Declining a preview's consent leaves the options open.
          if (_n.phase == VideoRequestPhase.declined) {
            _closed = true;
            Navigator.of(context).pop();
          }
        }
      case VideoRequestPhase.started:
        _closed = true;
        Navigator.of(context).pop();
        widget.onStarted(_n.jobId!);
      default:
        break;
    }
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _n,
      builder: (context, _) {
        final submitting = _n.phase == VideoRequestPhase.submitting;
        return PopScope(
          canPop: !submitting,
          child: AlertDialog(
            title: const Text('Create video'),
            content: SizedBox(
              width: 380,
              child: SingleChildScrollView(child: _body(context)),
            ),
            actions: [
              TextButton(
                onPressed:
                    submitting ? null : () => Navigator.of(context).pop(),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: _n.canSubmit ? () => _run(_n.submit) : null,
                child: const Text('Create video'),
              ),
            ],
          ),
        );
      },
    );
  }

  Widget _body(BuildContext context) {
    final theme = Theme.of(context);
    switch (_n.phase) {
      case VideoRequestPhase.loading when _n.consentProgress != null:
        return Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const CircularProgressIndicator(),
              const SizedBox(height: 16),
              Text(
                  'Decrypting tracks on this device: '
                  '${_n.consentProgress} of ${_n.consentIds.length}',
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodyMedium),
            ],
          ),
        );
      case VideoRequestPhase.loading:
      case VideoRequestPhase.submitting:
      case VideoRequestPhase.consentNeeded:
      case VideoRequestPhase.declined:
      case VideoRequestPhase.started:
        return const Padding(
          padding: EdgeInsets.all(24),
          child: Center(child: CircularProgressIndicator()),
        );
      case VideoRequestPhase.unavailable:
        return _optionsUnder(
          context,
          _notice(
            context,
            Icons.cloud_off_outlined,
            "Video rendering isn't available right now. Please try again later.",
          ),
        );
      case VideoRequestPhase.noneLeft:
        final q = _n.quota;
        return _optionsUnder(
          context,
          _notice(
            context,
            Icons.workspace_premium_outlined,
            q?.limit == null
                ? 'You have no videos left this month.'
                : "You've used all ${q!.limit} video${q.limit == 1 ? '' : 's'} "
                    'your plan includes this month.',
            color:
                theme.brightness == Brightness.dark ? kWarningDark : kWarning,
          ),
        );
      case VideoRequestPhase.error:
        return Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _notice(context, Icons.error_outline,
                _n.errorMessage ?? 'Something went wrong.',
                color: theme.colorScheme.error),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: () => _run(_n.loadPlan),
              child: const Text('Try again'),
            ),
          ],
        );
      case VideoRequestPhase.quotaExceeded:
      case VideoRequestPhase.ready:
        return _options(context);
    }
  }

  /// [notice] with the options under it, so a preview can still be made when
  /// no video can (previews are free, D3); [notice] alone without a plan.
  Widget _optionsUnder(BuildContext context, Widget notice) {
    if (_n.plan == null) return notice;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [notice, const SizedBox(height: 16), _options(context)],
    );
  }

  Widget _notice(BuildContext context, IconData icon, String text,
      {Color? color}) {
    final theme = Theme.of(context);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 20, color: color ?? theme.colorScheme.outline),
        const SizedBox(width: 10),
        Expanded(child: Text(text, style: theme.textTheme.bodyMedium)),
      ],
    );
  }

  Widget _options(BuildContext context) {
    final theme = Theme.of(context);
    final plan = _n.plan;
    if (plan == null) return const SizedBox.shrink();
    final quota = plan.quota;
    final dark = theme.brightness == Brightness.dark;
    final quotaError = _n.quotaError;

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Length', style: theme.textTheme.titleSmall),
        const SizedBox(height: 8),
        SegmentedButton<int>(
          segments: [
            for (final s in kVideoLengths)
              ButtonSegment(
                value: s,
                label: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('$s s'),
                    if (plan.clipCounts[s] != null)
                      Text(_clips(plan.clipCounts[s]!),
                          style: theme.textTheme.labelSmall),
                  ],
                ),
              ),
          ],
          selected: {_n.lengthS},
          showSelectedIcon: false,
          onSelectionChanged: (s) => _n.setLength(s.first),
        ),
        const SizedBox(height: 16),
        Text('Resolution', style: theme.textTheme.titleSmall),
        const SizedBox(height: 8),
        if (plan.resolutions.isNotEmpty)
          SegmentedButton<int>(
            segments: [
              for (final h in plan.resolutions)
                ButtonSegment(value: h, label: Text('${h}p')),
            ],
            selected: {_n.height ?? plan.resolutions.last},
            showSelectedIcon: false,
            onSelectionChanged: (s) => _n.setHeight(s.first),
          ),
        if (!plan.resolutions.contains(1080)) ...[
          const SizedBox(height: 4),
          Text('1080p is available on paid plans.',
              style: theme.textTheme.bodySmall),
        ],
        const SizedBox(height: 16),
        Text('Camera', style: theme.textTheme.titleSmall),
        const SizedBox(height: 8),
        RadioGroup<String>(
          groupValue: _n.cameraChoice,
          onChanged: (v) {
            if (v != null) _n.setCamera(v);
          },
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final (value, label, desc) in _cameras)
                RadioListTile<String>(
                  value: value,
                  contentPadding: EdgeInsets.zero,
                  title: Text(label),
                  subtitle: Text(desc, style: theme.textTheme.bodySmall),
                ),
            ],
          ),
        ),
        if (_n.cameraChoice == 'fixed')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: Text('Zoom out for flights and long legs',
                style: theme.textTheme.bodyMedium),
            value: _n.fixedZoomOut,
            onChanged: _n.setFixedZoomOut,
          ),
        const SizedBox(height: 8),
        _preview(context),
        const SizedBox(height: 16),
        Text(_quotaLine(quota), style: theme.textTheme.bodyMedium),
        if (plan.skipped > 0) ...[
          const SizedBox(height: 4),
          Text(
              '${plan.skipped} item${plan.skipped == 1 ? '' : 's'} of this '
              "trip can't be animated and will be left out.",
              style: theme.textTheme.bodySmall),
        ],
        if (_n.consentedCount > 0) ...[
          const SizedBox(height: 4),
          Text(
              'Includes ${_n.consentedCount} decrypted '
              'track${_n.consentedCount == 1 ? '' : 's'}, deleted after the '
              'render.',
              style: theme.textTheme.bodySmall),
        ],
        const SizedBox(height: 8),
        Text("We'll email you a link when your video is ready.",
            style: theme.textTheme.bodySmall),
        if (quotaError != null) ...[
          const SizedBox(height: 16),
          _notice(context, Icons.workspace_premium_outlined, quotaError.detail,
              color: dark ? kWarningDark : kWarning),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: () => showUpgradeSheet(context, quotaError),
            child: const Text('See plans'),
          ),
        ],
      ],
    );
  }

  /// The Preview button, where the preview is, and the preview itself once
  /// made (D9): greyed with a note when the settings changed since.
  Widget _preview(BuildContext context) {
    final theme = Theme.of(context);
    final bytes = _n.previewBytes;
    final outOfDate = _n.previewOutOfDate;
    final status = _previewStatus();
    final failed = switch (_n.previewPhase) {
      VideoPreviewPhase.failed ||
      VideoPreviewPhase.busy ||
      VideoPreviewPhase.tooLong ||
      VideoPreviewPhase.rateLimited ||
      VideoPreviewPhase.unavailable ||
      VideoPreviewPhase.error =>
        true,
      _ => false,
    };
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        OutlinedButton.icon(
          onPressed: _n.canPreview ? () => _run(_n.preview) : null,
          icon: const Icon(Icons.play_circle_outline),
          label: const Text('Preview'),
        ),
        if (status != null) ...[
          const SizedBox(height: 8),
          if (_n.previewInFlight)
            Row(
              children: [
                const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                const SizedBox(width: 10),
                Expanded(
                    child: Text(status, style: theme.textTheme.bodySmall)),
              ],
            )
          else
            _notice(context, failed ? Icons.error_outline : Icons.info_outline,
                status,
                color: failed ? theme.colorScheme.error : null),
        ],
        if (bytes != null) ...[
          const SizedBox(height: 8),
          AspectRatio(
            aspectRatio: 16 / 9,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(8),
              child: ColoredBox(
                color: theme.colorScheme.surfaceContainerHighest,
                child: VideoPreviewImage(bytes: bytes, dimmed: outOfDate),
              ),
            ),
          ),
          const SizedBox(height: 4),
          Text(
              outOfDate
                  ? 'Preview is for the previous settings'
                  : 'Low-resolution preview',
              style: theme.textTheme.bodySmall),
        ],
      ],
    );
  }

  /// The line under the Preview button, or null when there is nothing to say.
  String? _previewStatus() {
    switch (_n.previewPhase) {
      case VideoPreviewPhase.idle:
      case VideoPreviewPhase.done:
        return null;
      case VideoPreviewPhase.requesting:
        return 'Asking for a preview…';
      case VideoPreviewPhase.pending:
        return 'Waiting for a worker…';
      case VideoPreviewPhase.running:
        return 'Rendering preview…';
      case VideoPreviewPhase.failed:
        return "The preview couldn't be made — try again";
      case VideoPreviewPhase.busy:
        return 'Workers are busy — try again later';
      case VideoPreviewPhase.tooLong:
        return 'The preview took too long — try again';
      case VideoPreviewPhase.rateLimited:
        final s = _n.previewRetryAfterS;
        if (s == null) return 'Too many previews — try again later';
        final min = (s / 60).ceil().clamp(1, 60);
        return 'Too many previews — try again in $min min';
      case VideoPreviewPhase.unavailable:
        return 'Preview unavailable';
      case VideoPreviewPhase.error:
        return _n.previewError ?? 'Could not make the preview. Please try again.';
    }
  }

  /// Camera choices: the notifier's value, the title and the one-line
  /// description shown under it.
  static const _cameras = [
    ('variable', 'Follow', 'Zooms in and out to follow each leg.'),
    ('overview', 'Overview', 'Shows the whole trip; only the marker moves.'),
    ('fixed', 'Fixed zoom', 'Follows the route at one zoom level.'),
  ];

  static String _clips(int n) => '$n clip${n == 1 ? '' : 's'}';

  static String _quotaLine(VideoQuota q) {
    if (q.unlimited) return 'Unlimited videos on your plan.';
    final left = q.remaining ?? 0;
    return '$left of ${q.limit} video${q.limit == 1 ? '' : 's'} left this month.';
  }
}
