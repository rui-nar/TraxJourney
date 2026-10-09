/// Web implementation of [StravaOAuthPopup] — opens a blank popup window on
/// the click, then navigates it to the Strava OAuth flow, and listens for the
/// `postMessage` result sent by `web/oauth_callback.html` once the OAuth
/// redirect completes.
///
/// Message format from oauth_callback.html: a plain object
/// `{type: "strava_oauth", status: "code" | "error", code, state, reason}`
/// (docs/STRAVA_CONNECT_BINDING_PLAN.md, D8). Only messages from this
/// origin are read. A popup the user closes is reported by
/// [PopupResultArbiter], which also gives a message the time to arrive.
library;

import 'dart:js_interop';

import 'package:web/web.dart' as web;

import 'strava_popup_arbiter.dart';

export 'strava_popup_arbiter.dart' show StravaOAuthResult, StravaPopupHandle;

class StravaOAuthPopup {
  _WebHandle? _active;

  /// Opens a blank popup and starts listening for its result. Must be called
  /// synchronously from the click, so the browser keeps the user activation.
  /// Returns null when the browser blocked the popup.
  StravaPopupHandle? open() {
    dispose(); // Stop watching any previous attempt.

    final popup = web.window.open(
      '',
      'strava_oauth',
      'width=600,height=700,left=200,top=100',
    );
    if (popup == null) return null;

    return _active = _WebHandle(popup);
  }

  /// Stops watching the last opened popup. Safe to call even if [open] was
  /// never invoked or the attempt has already settled.
  void dispose() {
    _active?.detach();
    _active = null;
  }
}

class _WebHandle implements StravaPopupHandle {
  final web.Window _popup;
  late final PopupResultArbiter<StravaOAuthResult> _arbiter;
  // Must store as a JSFunction field so the same reference can be removed.
  late final JSFunction _messageHandler;

  _WebHandle(this._popup) {
    _arbiter =
        PopupResultArbiter<StravaOAuthResult>(isClosed: () => _popup.closed);
    _messageHandler = (web.Event event) {
      final msg = event as web.MessageEvent;
      if (msg.origin != web.window.origin) return;
      final data = msg.data.dartify();
      if (data is! Map || data['type'] != 'strava_oauth') return;

      final code = data['code'];
      final state = data['state'];
      final reason = data['reason'];
      _arbiter.onMessage(data['status'] == 'code' &&
              code is String &&
              code.isNotEmpty &&
              state is String &&
              state.isNotEmpty
          ? (code: code, state: state, error: null)
          : (
              code: null,
              state: null,
              error: reason is String && reason.isNotEmpty ? reason : 'failed',
            ));
    }.toJS;
    web.window.addEventListener('message', _messageHandler);
    _arbiter.result.then((message) {
      detach();
      if (message != null) _popup.close();
    });
  }

  @override
  void navigate(String url) => _popup.location.href = url;

  @override
  Future<StravaOAuthResult?> get result => _arbiter.result;

  @override
  void close() {
    detach();
    _popup.close();
  }

  /// Removes the listener and the timers; leaves the window as it is.
  void detach() {
    _arbiter.cancel();
    web.window.removeEventListener('message', _messageHandler);
  }
}
