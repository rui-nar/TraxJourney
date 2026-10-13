/// Loads Apple's sign-in script (appleid.auth.js) on demand (I1-3).
///
/// sign_in_with_apple_web calls the global `AppleID.auth.init` and
/// `AppleID.auth.signIn` the script defines, so it must be on the page before
/// a web sign-in. web/index.html no longer loads it, so that Apple only sees
/// visitors of the sign-in page, and only when it offers Sign in with Apple:
/// the login screen starts the load when it shows the button, and a tap that
/// beats the load waits for it.
library;

import 'package:flutter/foundation.dart' show kIsWeb, visibleForTesting;

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

class AppleJsLoader {
  AppleJsLoader(this._inject);

  final ScriptInjector _inject;
  Future<void>? _loading;
  bool _loaded = false;

  /// True once the script has run. A tap then calls Apple synchronously,
  /// inside the click, so the browser lets Apple open its popup.
  bool get isLoaded => _loaded;

  /// Injects the script once; later calls share the same load. A failed load
  /// is forgotten, so the next call tries again.
  Future<void> load() => _loading ??= _inject(appleJsUrl).then(
        (_) => _loaded = true,
        onError: (Object _) {
          _loading = null;
          throw const AppleJsLoadException();
        },
      );
}

/// Whether this build signs in through Apple JS. Not final so widget tests,
/// which never run as web, can say yes.
@visibleForTesting
bool appleNeedsJs = kIsWeb;

/// The page's loader. Not final so tests can swap in a fake injector.
@visibleForTesting
AppleJsLoader appleJsLoader = AppleJsLoader(injectScript);

/// Starts loading the script where Sign in with Apple needs it, ahead of the
/// tap. A failure is not shown here: the tap retries and reports it.
void preloadAppleJs() {
  if (!appleNeedsJs) return;
  appleJsLoader.load().catchError((Object _) {});
}

/// Whether a sign-in can call Apple now. Checked synchronously by the tap,
/// so a loaded script opens Apple's popup inside the click; otherwise the
/// tap waits for [loadAppleJs] first.
bool get appleJsReady => !appleNeedsJs || appleJsLoader.isLoaded;

/// Loads the script, or joins the load already running. Throws
/// [AppleJsLoadException] when it fails.
Future<void> loadAppleJs() => appleJsLoader.load();
