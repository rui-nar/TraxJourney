/// Landing screen for `/video/{token}` (docs/TRIP_VIDEO_PLAN.md, U7), the
/// link in the "your video is ready/failed" email — the counterpart of
/// `poster_download_screen.dart`. Opens with or without a session: the token
/// is the credential (`GET /api/video/{token}`), so this screen talks only to
/// the token routes.
///
/// "Download" hands the token URL to the browser or system player rather
/// than pulling the bytes in-app: the server answers range requests, so the
/// MP4 streams and seeks, and this works on every platform alike.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api/client.dart';
import '../api/video_api.dart';
import '../core/brand.dart';

/// Opens [url] outside the app; `launchUrl` in production.
typedef UrlOpener = Future<bool> Function(Uri url);

Future<bool> _openExternally(Uri url) =>
    launchUrl(url, mode: LaunchMode.externalApplication);

class VideoDownloadScreen extends StatefulWidget {
  final String token;

  /// Injectable for tests; production uses the shared [api] singleton.
  final ApiClient? apiClient;
  final UrlOpener openUrl;

  const VideoDownloadScreen({
    super.key,
    required this.token,
    this.apiClient,
    this.openUrl = _openExternally,
  });

  @override
  State<VideoDownloadScreen> createState() => _VideoDownloadScreenState();
}

class _VideoDownloadScreenState extends State<VideoDownloadScreen> {
  bool _loading = true;
  bool _notFound = false; // 404 — unknown token
  String? _error;
  VideoJobStatus? _status;

  @override
  void initState() {
    super.initState();
    _fetchStatus();
  }

  Future<void> _fetchStatus() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final s = await fetchVideoStatusByToken(
          token: widget.token, client: widget.apiClient);
      if (!mounted) return;
      setState(() {
        _loading = false;
        _status = s;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _notFound = e.statusCode == 404;
        _error = e.statusCode == 404 ? null : apiErrorDetail(e.body);
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Could not reach the server. Please try again.';
      });
    }
  }

  Future<void> _download() async {
    setState(() => _error = null);
    final ok = await widget
        .openUrl(videoDownloadUrl(widget.token, client: widget.apiClient));
    if (!ok && mounted) {
      setState(() => _error = 'Could not open the video.');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Card(
            margin: const EdgeInsets.all(24),
            child: Padding(
              padding: const EdgeInsets.all(32),
              child: _body(context),
            ),
          ),
        ),
      ),
    );
  }

  Widget _body(BuildContext context) {
    final theme = Theme.of(context);

    if (_loading) {
      return Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const CircularProgressIndicator(),
          const SizedBox(height: 20),
          Text('Checking your video…', style: theme.textTheme.bodyMedium),
        ],
      );
    }

    if (_notFound) {
      return _message(context, Icons.link_off,
          'This link is invalid or has expired', null,
          action: ElevatedButton(
            onPressed: () => context.go('/'),
            child: const Text('Go to $kAppName'),
          ));
    }

    final status = _status;
    final Widget main;
    if (status == null) {
      main = OutlinedButton(onPressed: _fetchStatus, child: const Text('Retry'));
    } else if (status.isDone) {
      main = _message(
        context,
        Icons.movie_outlined,
        'Your video is ready',
        _until(context, status.expiresAt),
        iconColor: theme.colorScheme.primary,
        action: ElevatedButton.icon(
          onPressed: _download,
          icon: const Icon(Icons.download),
          label: const Text('Download video'),
        ),
      );
    } else if (status.isFailed) {
      main = _message(
        context,
        Icons.error_outline,
        'Video generation failed',
        "Something went wrong making your video. It didn't count toward your "
            'monthly videos — please try again from the trip.',
        iconColor: theme.colorScheme.error,
      );
    } else if (status.isExpired) {
      main = _message(context, Icons.history, 'This video has expired',
          'Videos are kept for 30 days. You can make a new one from the trip.');
    } else {
      final pct = (status.progress * 100).round();
      main = _message(
        context,
        Icons.hourglass_top,
        'Still making your video…',
        [
          if (pct > 0) '$pct%',
          if (status.stage != null) status.stage!,
        ].join(' · '),
        action: OutlinedButton(
          onPressed: _fetchStatus,
          child: const Text('Refresh'),
        ),
      );
    }

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        main,
        if (_error != null) ...[
          const SizedBox(height: 12),
          Text(
            _error!,
            style: TextStyle(color: theme.colorScheme.error, fontSize: 12.5),
            textAlign: TextAlign.center,
          ),
        ],
      ],
    );
  }

  Widget _message(BuildContext context, IconData icon, String title,
      String? subtitle,
      {Color? iconColor, Widget? action}) {
    final theme = Theme.of(context);
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 44, color: iconColor ?? theme.colorScheme.outline),
        const SizedBox(height: 16),
        Text(title,
            style: theme.textTheme.titleMedium, textAlign: TextAlign.center),
        if (subtitle != null && subtitle.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(subtitle,
              style: theme.textTheme.bodySmall, textAlign: TextAlign.center),
        ],
        if (action != null) ...[
          const SizedBox(height: 20),
          action,
        ],
      ],
    );
  }

  static String? _until(BuildContext context, double? expiresAt) {
    if (expiresAt == null) return null;
    final d = DateTime.fromMillisecondsSinceEpoch((expiresAt * 1000).round());
    return 'Available until '
        '${MaterialLocalizations.of(context).formatMediumDate(d)}.';
  }
}
