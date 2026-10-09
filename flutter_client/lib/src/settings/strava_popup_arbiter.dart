/// Platform-neutral pieces of the Strava OAuth popup (docs/
/// STRAVA_CONNECT_FOLLOWUPS_PLAN.md, D3 and R2-3): the popup handle the
/// connect flow drives, and the arbiter that decides between a relayed
/// message and a closed popup. The decision lives here, not in the web file,
/// so it can be tested with `fakeAsync`.
library;

import 'dart:async';

/// Outcome of a Strava OAuth popup flow: the relayed [code] and [state], or
/// the callback's fixed [error] reason token.
typedef StravaOAuthResult = ({String? code, String? state, String? error});

/// An opened Strava popup, blank until [navigate] is called.
abstract class StravaPopupHandle {
  /// Points the popup at the Strava authorize [url].
  void navigate(String url);

  /// Completes once with the relayed result, or with null when the user
  /// closed the popup without one.
  Future<StravaOAuthResult?> get result;

  /// Closes the popup and stops watching it.
  void close();
}

/// Decides whether a popup attempt ended with a message or with a closed
/// popup, and reports exactly one of them through [result].
///
/// Every [checkInterval] it asks [isClosed]. The callback page posts its
/// message and closes in the same task, so `closed` can be seen before the
/// message is delivered: the first time [isClosed] is true it waits [grace]
/// longer, and a message in that time still wins (R1-2).
class PopupResultArbiter<T extends Object> {
  final bool Function() isClosed;
  final _completer = Completer<T?>();
  Timer? _check;
  Timer? _graceTimer;

  PopupResultArbiter({
    required this.isClosed,
    Duration checkInterval = const Duration(milliseconds: 500),
    Duration grace = const Duration(milliseconds: 500),
  }) {
    _check = Timer.periodic(checkInterval, (_) {
      if (_graceTimer != null || !isClosed()) return;
      _check?.cancel();
      _graceTimer = Timer(grace, () => _finish(null));
    });
  }

  /// The relayed message, or null when the popup was closed without one.
  Future<T?> get result => _completer.future;

  /// A message arrived. Ignored once the attempt has finished.
  void onMessage(T message) => _finish(message);

  /// Stops the timers without completing [result].
  void cancel() {
    _check?.cancel();
    _graceTimer?.cancel();
  }

  void _finish(T? value) {
    if (_completer.isCompleted) return;
    cancel();
    _completer.complete(value);
  }
}
