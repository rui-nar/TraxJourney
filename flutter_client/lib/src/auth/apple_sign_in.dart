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

  const AppleCredential({
    required this.idToken,
    required this.authorizationCode,
    required this.nonce,
    this.givenName,
    this.familyName,
  });
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
bool showAppleSignIn({
  bool isWeb = kIsWeb,
  TargetPlatform? platform,
  String? servicesId,
}) {
  final id = servicesId ?? appleServicesId;
  if (isWeb) return id.isNotEmpty;
  switch (platform ?? defaultTargetPlatform) {
    case TargetPlatform.iOS:
      return true;
    case TargetPlatform.android:
      return id.isNotEmpty;
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
Future<AppleCredential> signInWithApple() async {
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
  return AppleCredential(
    idToken: idToken,
    authorizationCode: credential.authorizationCode,
    nonce: rawNonce,
    givenName: _name(credential.givenName),
    familyName: _name(credential.familyName),
  );
}

/// Apple sends the name on the first sign-in only, possibly blank. The
/// server takes at most 100 characters; a longer name must not fail sign-in.
/// Cut on code points, so a surrogate pair is never split.
String? _name(String? name) {
  final trimmed = name?.trim() ?? '';
  if (trimmed.isEmpty) return null;
  return String.fromCharCodes(trimmed.runes.take(100));
}
