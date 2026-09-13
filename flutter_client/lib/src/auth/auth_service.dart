/// Authentication service — wraps /api/auth/* endpoints.
library;

import 'package:shared_preferences/shared_preferences.dart';

import '../api/client.dart';

class AuthService {
  static const _tokenKey = 'traxjourney_jwt';

  /// Where the token was kept before the TraxJourney rename (issue #151).
  /// [restoreSession] moves it to [_tokenKey] so existing users stay signed
  /// in. Once every active install has launched a release carrying this, the
  /// legacy read can be removed.
  static const _legacyTokenKey = 'viewtrip_jwt';

  /// Restore a persisted token on app start.
  Future<bool> restoreSession() async {
    final prefs = await SharedPreferences.getInstance();
    var token = prefs.getString(_tokenKey);
    final legacyToken = prefs.getString(_legacyTokenKey);
    if (legacyToken != null) {
      if (token == null) {
        token = legacyToken;
        await prefs.setString(_tokenKey, token);
      }
      await prefs.remove(_legacyTokenKey);
    }
    if (token != null) {
      api.setToken(token);
      return true;
    }
    return false;
  }

  Future<Map<String, dynamic>> loginWithPassword(
      String username, String password) async {
    final data = await api.post('/api/auth/token', {
      'username': username,
      'password': password,
    });
    await _persist(data['access_token'] as String);
    return data['user'] as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> loginWithGoogle(String idToken) async {
    final data = await api.post('/api/auth/google', {'id_token': idToken});
    await _persist(data['access_token'] as String);
    return data['user'] as Map<String, dynamic>;
  }

  /// Registers a new account. The server requires [email] to be a valid
  /// address — it is both the login identifier and where invite mail is sent
  /// (issue #110) — and builds the display name from [firstName] / [lastName].
  Future<Map<String, dynamic>> register(
    String email,
    String password, {
    required String firstName,
    required String lastName,
  }) async {
    final data = await api.post('/api/auth/register', {
      'username': email,
      'password': password,
      'first_name': firstName,
      'last_name': lastName,
    });
    await _persist(data['access_token'] as String);
    return data['user'] as Map<String, dynamic>;
  }

  /// POST /api/auth/verify-email — consumes an emailed verification token
  /// (issue #110). Unauthenticated: the recipient may click the link in a
  /// browser with no session. Throws [ApiException] 404 when the token is
  /// unknown, expired, or already used.
  Future<void> verifyEmail(String token) async {
    await api.post('/api/auth/verify-email', {'token': token});
  }

  /// POST /api/auth/resend-verification — reissues and resends the token,
  /// invalidating the previous one. Throws [ApiException] 429 when the
  /// caller has requested too many recently.
  Future<void> resendVerification() async {
    await api.post('/api/auth/resend-verification', {});
  }

  /// Fetches the current user's profile from the server.
  /// Requires a valid token to already be set on [api].
  Future<Map<String, dynamic>> getMe() async {
    final data = await api.get('/api/auth/me');
    return data as Map<String, dynamic>;
  }

  /// POST /api/auth/app-opened — best-effort "returnability" ping fired once
  /// per app launch. Unauthenticated: it also fires when no valid session
  /// was found, when there is no token to attach.
  Future<void> appOpened(String sessionState) async {
    await api.post('/api/auth/app-opened', {'session_state': sessionState});
  }

  Future<void> logout() async {
    api.clearToken();
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
    await prefs.remove(_legacyTokenKey);
  }

  /// Persist a token returned by a mid-session API call (e.g. profile update).
  Future<void> persistToken(String token) => _persist(token);

  Future<void> _persist(String token) async {
    api.setToken(token);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_tokenKey, token);
  }
}
