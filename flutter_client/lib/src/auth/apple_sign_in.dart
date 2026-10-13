/// Sign in with Apple (issue #580): the credential half of the flow. The
/// login screen hands what [signInWithApple] returns to
/// `AuthNotifier.loginWithApple`, which posts it to `POST /api/auth/apple`.
///
/// iOS uses the native sheet. Web (Apple JS popup) and Android (a Custom Tab
/// that returns through the server's callback) use Apple's web flow, which
/// needs the Services ID and a redirect URI on this deployment
/// (docs/STORE_DISTRIBUTION_PLAN.md, D1 and D5).
library;

import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart'
    show TargetPlatform, defaultTargetPlatform, kIsWeb, visibleForTesting;
import 'package:sign_in_with_apple/sign_in_with_apple.dart';

import '../api/client.dart';
import '../crypto/device_key_store.dart'
    show FlutterSecureKvStore, SecureKvStore;
import 'apple_js_loader.dart' show appleJsReady;

/// The Services ID web and Android sign in with. Empty when the build was
/// made without `--dart-define=APPLE_SERVICES_ID`, which hides the button on
/// those platforms. Not final so widget tests can set it.
@visibleForTesting
String appleServicesId = const String.fromEnvironment('APPLE_SERVICES_ID');

/// What the server needs from one Apple sign-in. [nonce] is the raw nonce:
/// Apple only ever saw its SHA-256 (D4).
class AppleCredential {
  final String idToken;
  final String authorizationCode;
  final String nonce;
  final String? givenName;
  final String? familyName;

  /// Apple's stable id for the user. Only the iOS sheet returns it; Apple JS
  /// (web) and the Android redirect leave it null.
  final String? userIdentifier;

  const AppleCredential({
    required this.idToken,
    required this.authorizationCode,
    required this.nonce,
    this.givenName,
    this.familyName,
    this.userIdentifier,
  });
}

/// Where a name Apple sent is kept until a sign-in carrying it succeeds
/// (R1-4). Not final so tests can swap in an in-memory store.
@visibleForTesting
SecureKvStore applePendingNameStore = FlutterSecureKvStore();

String _pendingNameKey(String appleUserId) =>
    'apple_pending_name_v1_$appleUserId';

/// The Apple user a kept name belongs to: the credential's userIdentifier
/// (iOS), else the id_token's `sub`, which is the same id (web, Android).
/// The token is only read, not verified: this is a local cache key, and the
/// server verifies the token. Null when neither is there or the token does
/// not parse.
String? _appleUserId(AppleCredential c) {
  final id = c.userIdentifier;
  if (id != null && id.isNotEmpty) return id;
  try {
    final parts = c.idToken.split('.');
    if (parts.length != 3) return null;
    final payload = jsonDecode(
        utf8.decode(base64Url.decode(base64Url.normalize(parts[1]))));
    final sub = payload is Map ? payload['sub'] : null;
    return sub is String && sub.isNotEmpty ? sub : null;
  } catch (_) {
    return null;
  }
}

/// Apple sends the name on the first authorization only. When that sign-in
/// fails to reach the server, the retry would carry none and the account
/// would be named after the relay address. So a name Apple sends is kept,
/// keyed by the Apple user ([_appleUserId]), and a later credential without
/// one gets it back. Without a user id nothing is kept. A storage failure
/// never blocks a sign-in: the credential goes as Apple gave it.
Future<AppleCredential> withPendingAppleName(AppleCredential c) async {
  final id = _appleUserId(c);
  if (id == null) return c;
  try {
    if (c.givenName != null || c.familyName != null) {
      await applePendingNameStore.write(
          _pendingNameKey(id),
          jsonEncode({
            'given_name': c.givenName,
            'family_name': c.familyName,
          }));
      return c;
    }
    final stored = await applePendingNameStore.read(_pendingNameKey(id));
    if (stored == null) return c;
    final names = jsonDecode(stored) as Map<String, dynamic>;
    return AppleCredential(
      idToken: c.idToken,
      authorizationCode: c.authorizationCode,
      nonce: c.nonce,
      givenName: names['given_name'] as String?,
      familyName: names['family_name'] as String?,
      userIdentifier: c.userIdentifier,
    );
  } catch (_) {
    return c;
  }
}

/// Drops the kept name once a sign-in has succeeded: the server has it now.
Future<void> forgetPendingAppleName(AppleCredential c) async {
  final id = _appleUserId(c);
  if (id == null) return;
  try {
    await applePendingNameStore.delete(_pendingNameKey(id));
  } catch (_) {
    // A leftover entry only resends a name the server no longer uses.
  }
}

/// The user dismissed Apple's sheet, popup or page. Not an error to show.
class AppleSignInCancelled implements Exception {
  const AppleSignInCancelled();
}

/// The plugin call, swapped out by tests. [nonce] is the hashed nonce;
/// [webAuthenticationOptions] is null on iOS.
typedef AppleCredentialRequester = Future<AuthorizationCredentialAppleID>
    Function({
  required String nonce,
  WebAuthenticationOptions? webAuthenticationOptions,
});

@visibleForTesting
AppleCredentialRequester requestAppleCredential = ({
  required String nonce,
  WebAuthenticationOptions? webAuthenticationOptions,
}) =>
    SignInWithApple.getAppleIDCredential(
      scopes: const [
        AppleIDAuthorizationScopes.email,
        AppleIDAuthorizationScopes.fullName,
      ],
      nonce: nonce,
      webAuthenticationOptions: webAuthenticationOptions,
    );

/// Whether the login screen offers Sign in with Apple. Always on an iOS
/// build, which uses the native sheet. On web and Android only when the
/// build carries a Services ID. Never on desktop, which ships neither.
///
/// [customServer] is true when a self-hosting URL override is saved. The web
/// flow then sends Apple a redirect URI on the self-hoster's host under
/// TraxJourney's Services ID, which Apple refuses, so web and Android hide
/// the button. iOS needs no redirect URI and is unaffected.
bool showAppleSignIn({
  bool isWeb = kIsWeb,
  TargetPlatform? platform,
  String? servicesId,
  bool customServer = false,
}) {
  final id = servicesId ?? appleServicesId;
  if (isWeb) return id.isNotEmpty && !customServer;
  switch (platform ?? defaultTargetPlatform) {
    case TargetPlatform.iOS:
      return true;
    case TargetPlatform.android:
      return id.isNotEmpty && !customServer;
    default:
      return false;
  }
}

/// Where Apple posts the web and Android result: this deployment's own
/// callback, the same URI the server sends with the code exchange
/// (APPLE_REDIRECT_URI). An empty [apiBaseUrl] means the web build posts
/// same-origin, so the page's origin stands in for it (R2-1).
Uri appleRedirectUri(String apiBaseUrl, Uri pageBase) {
  final origin = apiBaseUrl.isEmpty ? pageBase.origin : apiBaseUrl;
  return Uri.parse('$origin/api/auth/apple/callback');
}

/// 32 random bytes, base64url without padding.
String generateRawNonce() {
  final random = Random.secure();
  final bytes = List<int>.generate(32, (_) => random.nextInt(256));
  return base64Url.encode(bytes).replaceAll('=', '');
}

/// The value given to Apple, which lands in the id_token's `nonce` claim and
/// which the server recomputes from the raw nonce: lowercase hex SHA-256.
String hashNonce(String rawNonce) =>
    sha256.convert(utf8.encode(rawNonce)).toString();

/// Whether [e] means the user backed out. iOS and Android report a
/// cancellation code; Apple JS only reports an error string inside a generic
/// credentials exception.
bool isAppleSignInCancel(Object e) {
  if (e is SignInWithAppleAuthorizationException) {
    return e.code == AuthorizationErrorCode.canceled;
  }
  if (e is SignInWithAppleCredentialsException) {
    return e.message.contains('popup_closed_by_user') ||
        e.message.contains('user_cancelled_authorize');
  }
  return false;
}

/// Runs Apple's flow for this platform. Throws [AppleSignInCancelled] when
/// the user backs out, and any other exception when the flow fails.
///
/// On Android the future never completes if the user closes the Custom Tab;
/// the plugin rejects it when the next attempt starts.
///
/// A name Apple sends is kept until a sign-in succeeds, and a credential
/// without one gets the kept name back ([withPendingAppleName]).
///
/// On the web Apple JS must already be on the page ([appleJsReady]): Apple's
/// popup only opens inside the click, so this calls Apple before any await
/// and never waits for the script itself (I2-1). The login screen loads it.
Future<AppleCredential> signInWithApple() async {
  if (!appleJsReady) throw StateError("Apple's sign-in script is not loaded");
  final rawNonce = generateRawNonce();
  final nativeSheet = !kIsWeb && defaultTargetPlatform == TargetPlatform.iOS;
  final AuthorizationCredentialAppleID credential;
  try {
    credential = await requestAppleCredential(
      nonce: hashNonce(rawNonce),
      webAuthenticationOptions: nativeSheet
          ? null
          : WebAuthenticationOptions(
              clientId: appleServicesId,
              redirectUri: appleRedirectUri(api.baseUrl, Uri.base),
            ),
    );
  } catch (e) {
    if (isAppleSignInCancel(e)) throw const AppleSignInCancelled();
    rethrow;
  }
  final idToken = credential.identityToken;
  if (idToken == null || idToken.isEmpty) {
    throw Exception('no ID token received');
  }
  return withPendingAppleName(AppleCredential(
    idToken: idToken,
    authorizationCode: credential.authorizationCode,
    nonce: rawNonce,
    givenName: _name(credential.givenName),
    familyName: _name(credential.familyName),
    userIdentifier: credential.userIdentifier,
  ));
}

/// Apple sends the name on the first sign-in only, possibly blank. The
/// server takes at most 100 characters; a longer name must not fail sign-in.
/// Cut on code points, so a surrogate pair is never split.
String? _name(String? name) {
  final trimmed = name?.trim() ?? '';
  if (trimmed.isEmpty) return null;
  return String.fromCharCodes(trimmed.runes.take(100));
}
