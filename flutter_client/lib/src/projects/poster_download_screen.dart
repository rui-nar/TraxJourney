/// Poster download landing screen for `/poster/{token}` (issue #14).
///
/// Reached from the "your poster is ready/failed" notification email, signed
/// in or not — the poster export no longer blocks on a modal that polls the
/// job to completion (that UI lived in poster_job_notifier.dart, owned by a
/// parallel workstream); instead the server emails a link here once the job
/// reaches a terminal state. The link's token is itself the credential (see
/// `DBPosterJob.download_token` / `GET /api/poster/{token}` on the server),
/// so this screen talks to the API directly via [ApiClient] rather than
/// through any authenticated project service — it is deliberately
/// self-contained and does not import from poster_job_notifier.dart.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../api/client.dart';
import '../core/brand.dart';
import 'download_stub.dart' if (dart.library.html) 'download_web.dart';

/// Extracts the server's `detail` message out of an exception, mirroring
/// verifyErrorMessage in verify_email_screen.dart.
String _errorMessage(Object e) {
  final s = e.toString();
  final m = RegExp(r'"detail"\s*:\s*"([^"]+)"').firstMatch(s);
  return m?.group(1) ?? s.replaceFirst('Exception: ', '');
}

class PosterDownloadScreen extends StatefulWidget {
  final String token;
  final ApiClient apiClient;

  PosterDownloadScreen({super.key, required this.token, ApiClient? apiClient})
      : apiClient = apiClient ?? ApiClient();

  @override
  State<PosterDownloadScreen> createState() => _PosterDownloadScreenState();
}

class _PosterDownloadScreenState extends State<PosterDownloadScreen> {
  bool _loading = true;
  bool _notFound = false; // 404 — unknown or expired token
  String? _error;
  String? _status; // pending | running | done | failed
  String? _stage;
  String? _downloading; // 'png' | 'pdf' while a download is in flight

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
      final body = await widget.apiClient.get('/api/poster/${widget.token}')
          as Map<String, dynamic>;
      if (!mounted) return;
      setState(() {
        _loading = false;
        _status = body['status'] as String?;
        _stage = body['stage'] as String?;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _notFound = e.statusCode == 404;
        _error = e.statusCode == 404 ? null : _errorMessage(e);
      });
    } catch (e) {
      // Not just ApiException: an unreachable/misconfigured HTTP client can
      // throw other errors too — never let the status poll crash the screen.
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = _errorMessage(e);
      });
    }
  }

  Future<void> _download(String format) async {
    setState(() {
      _downloading = format;
      _error = null;
    });
    try {
      final res = await widget.apiClient
          .getRaw('/api/poster/${widget.token}/download?format=$format');
      final mimeType = format == 'png' ? 'image/png' : 'application/pdf';
      triggerBrowserDownload(res.bodyBytes, mimeType, 'poster.$format');
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = _errorMessage(e));
    } finally {
      if (mounted) setState(() => _downloading = null);
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
          Text('Checking your poster…', style: theme.textTheme.bodyMedium),
        ],
      );
    }

    if (_notFound) {
      return Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.link_off, size: 44, color: theme.colorScheme.outline),
          const SizedBox(height: 16),
          Text('This link is invalid or has expired',
              style: theme.textTheme.titleMedium,
              textAlign: TextAlign.center),
          const SizedBox(height: 20),
          ElevatedButton(
            onPressed: () => context.go('/'),
            child: const Text('Go to $kAppName'),
          ),
        ],
      );
    }

    final status = _status;
    final children = <Widget>[];

    if (status == 'done') {
      children.addAll([
        Icon(Icons.picture_as_pdf_outlined,
            size: 44, color: theme.colorScheme.primary),
        const SizedBox(height: 16),
        Text('Your poster is ready', style: theme.textTheme.titleMedium),
        const SizedBox(height: 20),
        ElevatedButton(
          onPressed: _downloading != null ? null : () => _download('png'),
          child: _downloading == 'png'
              ? const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Download PNG'),
        ),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: _downloading != null ? null : () => _download('pdf'),
          child: _downloading == 'pdf'
              ? const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Download PDF'),
        ),
      ]);
    } else if (status == 'failed') {
      children.addAll([
        Icon(Icons.error_outline, size: 44, color: theme.colorScheme.error),
        const SizedBox(height: 16),
        Text('Poster generation failed', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        Text(
          'Something went wrong generating your poster. Please try again '
          'from the trip.',
          textAlign: TextAlign.center,
          style: theme.textTheme.bodySmall,
        ),
      ]);
    } else {
      // pending | running — no client-side poll loop for a one-off status
      // page; a manual refresh is simpler and sufficient here.
      children.addAll([
        const SizedBox(
            width: 28,
            height: 28,
            child: CircularProgressIndicator(strokeWidth: 2)),
        const SizedBox(height: 16),
        Text('Still generating your poster…',
            style: theme.textTheme.titleMedium,
            textAlign: TextAlign.center),
        if (_stage != null) ...[
          const SizedBox(height: 4),
          Text(_stage!,
              style: theme.textTheme.bodySmall, textAlign: TextAlign.center),
        ],
        const SizedBox(height: 20),
        OutlinedButton(
          onPressed: _fetchStatus,
          child: const Text('Refresh'),
        ),
      ]);
    }

    if (_error != null) {
      children.addAll([
        const SizedBox(height: 12),
        Text(
          _error!,
          style: TextStyle(color: theme.colorScheme.error, fontSize: 12.5),
          textAlign: TextAlign.center,
        ),
      ]);
    }

    return Column(mainAxisSize: MainAxisSize.min, children: children);
  }
}
