/// Loads Apple's sign-in script (appleid.auth.js) on demand (I1-3).
///
/// sign_in_with_apple_web calls the global `AppleID.auth.init` and
/// `AppleID.auth.signIn` the script defines, so it must be on the page before
/// a web sign-in. web/index.html no longer loads it, so that Apple only sees
/// visitors of the sign-in page, and only when it offers Sign in with Apple:
/// the login screen starts the load when it shows the button.
///
/// Apple's popup only opens inside the user's click, so a tap may only call
/// Apple once the script is there (I2-1). The login screen follows
/// [appleJsState]: the button is disabled while the script loads, and a tap
/// before it has loaded (after a failure) only retries the load.
library;

import 'package:flutter/foundation.dart'
    show ValueListenable, ValueNotifier, kIsWeb, visibleForTesting;

import 'apple_js_inject_stub.dart'
    if (dart.library.js_interop) 'apple_js_inject_web.dart';

const appleJsUrl = 'https://appleid.cdn-apple.com/appleauth/static/jsapi/'
    'appleid/1/en_US/appleid.auth.js';

/// Adds a `<script src>` to the page; completes on its load event and fails
/// on its error event.
typedef ScriptInjector = Future<void> Function(String src);

/// The script could not be loaded (offline, blocked by an extension or a
/// network filter). Shown to the user as the reason the sign-in failed.
class AppleJsLoadException implements Exception {
  const AppleJsLoadException();

  @override
  String toString() => "Apple's sign-in script could not be loaded";
}

/// Where the script's load stands. [idle] until the first load starts.
enum AppleJsState { idle, loading, loaded, failed }

class AppleJsLoader {
  AppleJsLoader(this._inject);

  final ScriptInjector _inject;
  Future<void>? _loading;
  final _state = ValueNotifier(AppleJsState.idle);

  ValueListenable<AppleJsState> get state => _state;

  /// True once the script has run. A tap then calls Apple synchronously,
  /// inside the click, so the browser lets Apple open its popup.
  bool get isLoaded => _state.value == AppleJsState.loaded;

  /// Injects the script once; later calls share the same load. A failed load
  /// is forgotten, so the next call tries again.
  Future<void> load() {
    final running = _loading;
    if (running != null) return running;
    _state.value = AppleJsState.loading;
    return _loading = _inject(appleJsUrl).then(
      (_) => _state.value = AppleJsState.loaded,
      onError: (Object _) {
        _loading = null;
        _state.value = AppleJsState.failed;
        throw const AppleJsLoadException();
      },
    );
  }
}

/// Whether this build signs in through Apple JS. Not final so widget tests,
/// which never run as web, can say yes.
@visibleForTesting
bool appleNeedsJs = kIsWeb;

/// The page's loader. Not final so tests can swap in a fake injector.
@visibleForTesting
AppleJsLoader appleJsLoader = AppleJsLoader(injectScript);

/// Starts loading the script where Sign in with Apple needs it, ahead of the
/// tap. A failure is not shown here: it moves [appleJsState] to failed, and
/// the tap retries and reports it.
void preloadAppleJs() {
  if (!appleNeedsJs) return;
  appleJsLoader.load().catchError((Object _) {});
}

/// The script's load, for the login screen's Apple button. Only meaningful
/// where [appleNeedsJs]; elsewhere it stays idle.
ValueListenable<AppleJsState> get appleJsState => appleJsLoader.state;

/// Whether the Apple button must stay disabled: the script is loading, and a
/// tap now could only open Apple's popup after the click is over.
bool get appleJsLoading =>
    appleNeedsJs && appleJsLoader.state.value == AppleJsState.loading;

/// Whether a sign-in can call Apple now. Checked synchronously by the tap,
/// so a loaded script opens Apple's popup inside the click; otherwise the
/// tap only runs [loadAppleJs].
bool get appleJsReady => !appleNeedsJs || appleJsLoader.isLoaded;

/// Loads the script, or joins the load already running. Throws
/// [AppleJsLoadException] when it fails.
Future<void> loadAppleJs() => appleJsLoader.load();
