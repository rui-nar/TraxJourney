/// App-wide guard against stale cached web bundles.
///
/// A returning web user can be served an old `main.dart.js` from cache after a
/// deploy (the bug that surfaced as a wrong baked API URL). This widget compares
/// the client's build-time [APP_VERSION] against the server's `/api/version` and,
/// when they differ, shows a non-dismissable "new version available" bar with a
/// Reload button — turning a silent breakage into a one-tap refresh.
library;

import 'dart:async';

import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api/client.dart';
import 'app_version.dart';
import 'brand.dart';
import 'version_reload_stub.dart'
    if (dart.library.js_interop) 'version_reload_web.dart';

/// True when the running client bundle is older/different than what the server
/// reports — i.e. the user is on a stale build and should reload.
///
/// Conservative on purpose: never fires when either side is missing or `dev`
/// (local builds), so it only triggers between two real, differing deployments.
bool isClientStale(String clientVersion, String serverVersion) {
  if (clientVersion.isEmpty || serverVersion.isEmpty) return false;
  if (clientVersion == 'dev' || serverVersion == 'dev') return false;
  return clientVersion != serverVersion;
}

/// Where an installed APK is updated from: the latest GitHub release (there is
/// no app store and no iOS build; see docs/ANDROID.md).
final Uri kUpdateUrl =
    Uri.parse('https://github.com/rui-nar/TraxJourney/releases/latest');

/// Parses `x.y.z` (an optional leading `v`, any suffix after the third number
/// ignored) into three integers, or null when it is not that shape.
List<int>? _parseVersion(String v) {
  final m = RegExp(r'^v?(\d+)\.(\d+)\.(\d+)').firstMatch(v.trim());
  if (m == null) return null;
  return [for (var i = 1; i <= 3; i++) int.parse(m.group(i)!)];
}

/// True when [client] is numerically older than the server's
/// `min_client_version` ([minimum]).
///
/// Conservative like [isClientStale]: `dev`, empty and unparsable values on
/// either side return false, so a local build or a server that does not send
/// the field never blocks anyone. The default minimum `0.0.0` is never above
/// any client.
bool isBelowMinimum(String client, String minimum) {
  final c = _parseVersion(client);
  final m = _parseVersion(minimum);
  if (c == null || m == null) return false;
  for (var i = 0; i < 3; i++) {
    if (c[i] != m[i]) return c[i] < m[i];
  }
  return false;
}

class VersionGate extends StatefulWidget {
  final Widget child;

  /// The build's own version and platform; overridable only so tests can drive
  /// both branches, since the real values are compile-time constants.
  final String clientVersion;
  final bool isWeb;

  const VersionGate({
    super.key,
    required this.child,
    this.clientVersion = kClientVersion,
    this.isWeb = kIsWeb,
  });

  @override
  State<VersionGate> createState() => _VersionGateState();
}

class _VersionGateState extends State<VersionGate> with WidgetsBindingObserver {
  bool _stale = false;
  bool _belowMin = false;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    // The check runs everywhere, because its result also feeds [serverVersion],
    // which every version label in the app reads (issue #275). Re-checking on
    // resume is what lets a client that started offline fill that in later.
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) => _check());
    // Periodic re-check so a long-lived tab notices a deploy without a manual
    // reload; cheap (one tiny GET). Web only — a native app is not serving
    // itself a cached bundle, so there is nothing for it to notice mid-session.
    if (widget.isWeb) {
      _timer = Timer.periodic(const Duration(minutes: 15), (_) => _check());
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _check();
  }

  Future<void> _check() async {
    if (_stale || !mounted) return;
    try {
      final data = await api.get('/api/version') as Map<String, dynamic>;
      final version = (data['version'] as String?) ?? '';
      if (!mounted) return;
      if (version.isNotEmpty) serverVersion.value = version;
      // The stale-bundle prompt stays web-only: a native app updates through
      // its store, is expected to lag the server, and cannot reload itself.
      if (widget.isWeb && isClientStale(widget.clientVersion, version)) {
        setState(() => _stale = true);
      }
      // The minimum applies everywhere: a build below it can no longer be
      // trusted to talk to this server (Decision 15). Re-evaluated on every
      // check, so a lowered minimum releases the block.
      final below = isBelowMinimum(
          widget.clientVersion, (data['min_client_version'] as String?) ?? '');
      if (below != _belowMin) setState(() => _belowMin = below);
    } catch (_) {
      // Network blip — ignore; we'll try again on the next tick/resume.
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    // Native cannot reload itself, so a too-old build gets a screen instead of
    // a bar, and the app behind it is not reachable.
    if (_belowMin && !widget.isWeb) return const _UpdateRequiredScreen();
    if (!_stale && !_belowMin) return widget.child;
    final scheme = Theme.of(context).colorScheme;
    return Stack(
      children: [
        widget.child,
        Positioned(
          left: 0,
          right: 0,
          bottom: 0,
          child: SafeArea(
            child: Material(
              color: scheme.inverseSurface,
              elevation: 6,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 10, 8, 10),
                child: Row(
                  children: [
                    Icon(Icons.system_update_alt,
                        size: 20, color: scheme.onInverseSurface),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(
                        _belowMin
                            ? 'This version of $kAppName is no longer supported. Please reload.'
                            : 'A new version of $kAppName is available.',
                        style: TextStyle(color: scheme.onInverseSurface),
                      ),
                    ),
                    TextButton(
                      onPressed: reloadApp,
                      child: Text(
                        'Reload',
                        style: TextStyle(
                          color: scheme.inversePrimary,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

/// Full-screen block for a native build below the server's minimum version.
class _UpdateRequiredScreen extends StatelessWidget {
  const _UpdateRequiredScreen();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.system_update_alt,
                    size: 48, color: theme.colorScheme.primary),
                const SizedBox(height: 16),
                Text('Update required', style: theme.textTheme.headlineSmall),
                const SizedBox(height: 8),
                Text(
                  'This version of $kAppName is no longer supported. '
                  'Please install the latest version to continue.',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 24),
                FilledButton(
                  onPressed: () => launchUrl(kUpdateUrl,
                      mode: LaunchMode.externalApplication),
                  child: const Text('Get the update'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
