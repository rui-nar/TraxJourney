/// Dialog for requesting a trip video (docs/TRIP_VIDEO_PLAN.md, U7): pick a
/// length (30/60/90 s, D11), a resolution the plan allows (D10) and a camera
/// (docs/VIDEO_CAMERA_QUALITY_PLAN.md, D1), see the monthly quota (D3), then
/// create. Renders a [VideoRequestNotifier] and runs
/// its consent step (D2) through [showVideoConsentDialog].
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

class VideoConfigDialog extends StatefulWidget {
  final ProjectRef projectRef;
  final List<Map<String, dynamic>> Function() activities;
  final void Function(int jobId) onStarted;

  /// Injectable for tests; production uses the shared [api] singleton.
  final ApiClient? client;
  final FieldRevealer? reveal;
  final TrackFetcher? fetchTrack;

  const VideoConfigDialog({
    super.key,
    required this.projectRef,
    required this.activities,
    required this.onStarted,
    this.client,
    this.reveal,
    this.fetchTrack,
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
  );

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
    if (!mounted) return;
    switch (_n.phase) {
      case VideoRequestPhase.consentNeeded:
        final ok = await showVideoConsentDialog(context,
            activityCount: _n.consentIds.length);
        if (!mounted) return;
        if (ok) {
          await _run(_n.acceptConsent);
        } else {
          _n.declineConsent();
          Navigator.of(context).pop();
        }
      case VideoRequestPhase.started:
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
        return _notice(
          context,
          Icons.cloud_off_outlined,
          "Video rendering isn't available right now. Please try again later.",
        );
      case VideoRequestPhase.noneLeft:
        final q = _n.quota;
        return _notice(
          context,
          Icons.workspace_premium_outlined,
          q?.limit == null
              ? 'You have no videos left this month.'
              : "You've used all ${q!.limit} video${q.limit == 1 ? '' : 's'} "
                  'your plan includes this month.',
          color: theme.brightness == Brightness.dark ? kWarningDark : kWarning,
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
        SegmentedButton<String>(
          segments: [
            for (final (value, label, _) in _cameras)
              ButtonSegment(value: value, label: Text(label)),
          ],
          selected: {_n.cameraChoice},
          showSelectedIcon: false,
          onSelectionChanged: (s) => _n.setCamera(s.first),
        ),
        const SizedBox(height: 4),
        Text(
            _cameras.firstWhere((c) => c.$1 == _n.cameraChoice).$3,
            style: theme.textTheme.bodySmall),
        if (_n.cameraChoice == 'fixed')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: Text('Zoom out for flights and long legs',
                style: theme.textTheme.bodyMedium),
            value: _n.fixedZoomOut,
            onChanged: _n.setFixedZoomOut,
          ),
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

  /// Camera choices: the notifier's value, the label and the helper line.
  static const _cameras = [
    ('variable', 'Variable', 'Zooms in and out to follow each leg.'),
    ('overview', 'Overview', 'Shows the whole trip for the whole video.'),
    ('fixed', 'Fixed zoom', 'Follows the route at one zoom level.'),
  ];

  static String _clips(int n) => '$n clip${n == 1 ? '' : 's'}';

  static String _quotaLine(VideoQuota q) {
    if (q.unlimited) return 'Unlimited videos on your plan.';
    final left = q.remaining ?? 0;
    return '$left of ${q.limit} video${q.limit == 1 ? '' : 's'} left this month.';
  }
}
