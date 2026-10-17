/// Initialises Google Sign-In on first use, from the login screen (#611).
///
/// On the web, initialising is what loads Google's sign-in script
/// (accounts.google.com/gsi/client; the vendored google_sign_in_web in
/// third_party/ no longer loads it at plugin registration). Only the login
/// screen calls [ensureGoogleSignInInitialized], so a visitor who never opens
/// it, such as one following a share link, sends nothing to Google.
library;

import 'package:flutter/foundation.dart' show kIsWeb, visibleForTesting;
import 'package:google_sign_in/google_sign_in.dart';

// Server client ID used on Android/iOS to receive an idToken.
// On web, GIS reads the client ID from <meta name="google-signin-client_id">
// in web/index.html — so serverClientId must be null on web.
const _kGoogleServerClientId =
    '544571555396-gj0q3hndadfo00ifotme305jcf4ii5cc.apps.googleusercontent.com';

Future<void>? _initialized;

/// Calls [GoogleSignIn.initialize] once per app run; later calls return the
/// same future. The plugin refuses a second initialize(), so a failed one is
/// not retried.
Future<void> ensureGoogleSignInInitialized() =>
    _initialized ??= GoogleSignIn.instance.initialize(
      serverClientId: kIsWeb ? null : _kGoogleServerClientId,
    );

/// Forgets the first call, so each test starts uninitialised.
@visibleForTesting
void resetGoogleSignInInitialized() => _initialized = null;
