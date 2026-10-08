/// Non-web fallback for [StravaOAuthPopup].
///
/// The OAuth popup + postMessage handshake is a web-only flow — on other
/// platforms, `_connectStrava()` in settings_screen.dart takes the
/// `launchUrl()` branch instead and never calls into this class. It exists
/// only so `StravaConnectFlow` compiles under non-web platforms
/// (including `flutter test`'s VM platform).
library;

import 'strava_popup_arbiter.dart';

export 'strava_popup_arbiter.dart' show StravaOAuthResult, StravaPopupHandle;

class StravaOAuthPopup {
  StravaPopupHandle? open() => _StubHandle();

  void dispose() {}
}

class _StubHandle implements StravaPopupHandle {
  @override
  void navigate(String url) {}

  @override
  Future<StravaOAuthResult?> get result async =>
      (code: null, state: null, error: 'web_only');

  @override
  void close() {}
}
