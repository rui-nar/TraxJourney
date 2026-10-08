/// Web implementation of [StravaOAuthPopup] — opens the Strava OAuth flow in
/// a popup window and listens for the `postMessage` result sent by
/// `web/oauth_callback.html` once the OAuth redirect completes.
///
/// Message format from oauth_callback.html: a plain object
/// `{type: "strava_oauth", status: "code" | "error", code, state, reason}`
/// (docs/STRAVA_CONNECT_BINDING_PLAN.md, D8). Only messages from this
/// origin are read.
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart' as web;

/// Outcome of a Strava OAuth popup flow: the relayed [code] and [state], or
/// the callback's fixed [error] reason token.
typedef StravaOAuthResult = ({String? code, String? state, String? error});

class StravaOAuthPopup {
  JSFunction? _messageHandler;

  /// Opens [url] in a popup and resolves once `web/oauth_callback.html`
  /// posts back the OAuth result. Removes the listener and closes the popup
  /// before resolving.
  Future<StravaOAuthResult> connect(String url) {
    dispose(); // Remove any stale listener from a previous attempt.

    final popup = web.window.open(
      url,
      'strava_oauth',
      'width=600,height=700,left=200,top=100',
    );

    final completer = Completer<StravaOAuthResult>();

    // Must store as a JSFunction field so the same reference can be removed.
    _messageHandler = (web.Event event) {
      final msg = event as web.MessageEvent;
      if (msg.origin != web.window.origin) return;
      final data = msg.data.dartify();
      if (data is! Map || data['type'] != 'strava_oauth') return;

      dispose();
      popup?.close();

      final code = data['code'];
      final state = data['state'];
      final reason = data['reason'];
      final StravaOAuthResult result = data['status'] == 'code' &&
              code is String &&
              code.isNotEmpty &&
              state is String &&
              state.isNotEmpty
          ? (code: code, state: state, error: null)
          : (
              code: null,
              state: null,
              error: reason is String && reason.isNotEmpty ? reason : 'failed',
            );

      if (!completer.isCompleted) completer.complete(result);
    }.toJS;

    web.window.addEventListener('message', _messageHandler!);

    return completer.future;
  }

  /// Removes any pending message listener. Safe to call even if [connect]
  /// was never invoked or has already settled.
  void dispose() {
    if (_messageHandler != null) {
      web.window.removeEventListener('message', _messageHandler!);
      _messageHandler = null;
    }
  }
}
