/// Where Android brings the user back after Strava consent in the system
/// browser (docs/STRAVA_CONNECT_BINDING_PLAN.md, D4 and U3).
///
/// The callback sends the browser to
/// `traxjourney://app/strava-return?strava=code&code=…&state=…`, or
/// `?strava=error&reason=…`, which the router delivers here. A relayed code
/// finishes the pending connect through [stravaConnect]. Its outcome is
/// already shown app-wide (showStravaConnectOutcomes), so this screen shows
/// nothing of its own for it. A relayed error has no connect to finish, so it
/// is shown here, on the root messenger, which outlives this route. Either way
/// the user lands on Settings.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../auth/auth_notifier.dart';
import 'strava_connect_flow.dart';

/// The path of `traxjourney://app/strava-return`, as the router sees it.
const String kStravaReturnRoute = '/strava-return';

class StravaReturnScreen extends StatefulWidget {
  final String? code;
  final String? state;

  /// `code` or `error`, as the callback relayed it.
  final String? strava;

  /// The callback's fixed reason token when [strava] is `error`.
  final String? reason;

  const StravaReturnScreen({
    super.key,
    this.code,
    this.state,
    this.strava,
    this.reason,
  });

  @override
  State<StravaReturnScreen> createState() => _StravaReturnScreenState();
}

class _StravaReturnScreenState extends State<StravaReturnScreen> {
  late final AuthNotifier _auth;
  bool _started = false;

  @override
  void initState() {
    super.initState();
    // On a cold start this route is built under the splash while the session
    // is still being restored, and completing needs the restored bearer. So
    // wait for the restore to end (R1-1).
    _auth = context.read<AuthNotifier>()..addListener(_maybeFinish);
    WidgetsBinding.instance.addPostFrameCallback((_) => _maybeFinish());
  }

  @override
  void dispose() {
    _auth.removeListener(_maybeFinish);
    super.dispose();
  }

  void _maybeFinish() {
    if (_started || !mounted || _auth.isRestoring) return;
    // Restored signed out: the router's redirect takes the user to login.
    if (_auth.user == null) return;
    _started = true;
    _auth.removeListener(_maybeFinish);
    _finish();
  }

  Future<void> _finish() async {
    final messenger = ScaffoldMessenger.of(context);
    void show(StravaConnectOutcome outcome) => messenger.showSnackBar(
        SnackBar(content: Text(stravaConnectMessage(outcome))));

    final code = widget.code;
    final state = widget.state;
    if (widget.strava != 'error' && code != null && state != null) {
      try {
        await stravaConnect.complete(code, state);
      } catch (_) {
        // complete() maps every server answer to an outcome itself. Only a
        // secure-storage failure lands here, and it must not strand the user
        // on the spinner.
        show(StravaConnectOutcome.failed);
      }
    } else {
      show(stravaOutcomeForReason(widget.reason));
    }
    if (mounted) context.go('/settings');
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const CircularProgressIndicator(),
            const SizedBox(height: 20),
            Text('Connecting Strava…', style: theme.textTheme.bodyMedium),
          ],
        ),
      ),
    );
  }
}
