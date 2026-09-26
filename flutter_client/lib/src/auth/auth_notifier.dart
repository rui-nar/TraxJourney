import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import '../api/client.dart';
import '../crypto/encryption.dart';
import '../projects/photo_thumb_cache.dart';
import '../projects/project_data_cache.dart';
import 'auth_service.dart';

// ── User model ─────────────────────────────────────────────────────────────────

class User {
  final String id;
  final String email;
  final String displayName;
  final String avatarUrl;
  final String authProvider; // "local" | "google"
  final bool isAdmin;

  /// Whether the account's email address has been confirmed (issue #110).
  /// Gates sending invite mail and being matched to a pending invite.
  final bool emailVerified;
  final bool passwordChangeRequired;

  const User({
    required this.id,
    required this.email,
    required this.displayName,
    required this.avatarUrl,
    required this.authProvider,
    this.isAdmin = false,
    this.emailVerified = false,
    this.passwordChangeRequired = false,
  });

  factory User.fromMap(Map<String, dynamic> map) => User(
        id: map['id']?.toString() ?? '',
        email: map['email'] as String? ?? '',
        displayName: map['display_name'] as String? ?? '',
        avatarUrl: map['avatar_url'] as String? ?? '',
        authProvider: map['auth_provider'] as String? ?? 'local',
        isAdmin: map['is_admin'] == true,
        emailVerified: map['email_verified'] == true,
        passwordChangeRequired: map['password_change_required'] == true,
      );

  /// Sentinel used after restoring a session from secure storage when no
  /// /api/auth/me endpoint is available yet to fetch the full profile.
  static const restored = User(
    id: '',
    email: '',
    displayName: '',
    avatarUrl: '',
    authProvider: 'local',
  );
}

// ── AuthNotifier ───────────────────────────────────────────────────────────────

class AuthNotifier extends ChangeNotifier {
  final AuthService _service;

  User? _user;
  bool _isLoading = false;
  bool _isRestoring = false;
  String? _error;

  AuthNotifier(this._service);

  User? get user => _user;
  bool get isLoading => _isLoading;

  /// Keeps [projectDataCache] scoped to whoever is currently signed in —
  /// every codepath that changes [_user] eventually calls [notifyListeners],
  /// so hooking it here covers login/restore/register/profile-refresh
  /// without a separate call at each of those sites. Idempotent/cheap when
  /// the user hasn't actually changed.
  @override
  void notifyListeners() {
    final idStr = _user?.id;
    projectDataCache.setCurrentUser(
        (idStr == null || idStr.isEmpty) ? null : int.tryParse(idStr));
    super.notifyListeners();
  }

  /// True only while [init] restores a persisted session at app start.
  ///
  /// [isLoading] cannot stand in for this: it is also raised by an interactive
  /// sign-in, and SplashGate keys the brand splash off this flag — it must not
  /// come back over the login form once the app is up.
  bool get isRestoring => _isRestoring;
  String? get error => _error;

  /// Below this much remaining life on a restored token, [init] falls back to
  /// blocking on getMe() to find out for sure whether it's still valid.
  /// Tokens are minted with a 7-day lifetime (see api/deps.py), so this buffer
  /// isn't there to catch near-term revocation — the still-fired background
  /// getMe() below does that within the same session — it's just a guard
  /// against clock skew and the rare relaunch that lands right at expiry.
  static const _kMinTokenLifetime = Duration(minutes: 5);

  /// Restores a persisted session on app start.
  /// Call via `..init()` cascade in Provider `create`.
  Future<void> init() async {
    _isLoading = true;
    _isRestoring = true;
    notifyListeners();
    try {
      final restored = await _service.restoreSession();
      if (restored) {
        final token = api.tokenForUpload;
        final expiry = token == null ? null : _jwtExpiry(token);
        final comfortablyValid = expiry != null &&
            expiry.difference(DateTime.now().toUtc()) > _kMinTokenLifetime;
        if (comfortablyValid) {
          // Plenty of life left on the token: trust it locally instead of
          // blocking app start on the network. getMe() still runs in the
          // background (see _refreshMeInBackground) to catch server-side
          // revocation, email_verified changes, or a 401 forcing logout.
          _user = User.restored;
          _recordAppOpened('resumed');
          unawaited(_refreshMeInBackground());
        } else {
          // Token missing/unparseable/at-or-near expiry — fall back to the
          // original blocking behavior so we find out for sure.
          // A 401 means the token is expired/invalid: clear it and force
          // re-login. Any other error (no network, timeout) keeps the
          // sentinel user so the session remains valid offline.
          try {
            final data = await _service.getMe();
            _user = User.fromMap(data);
            await _unlockEncryption();
            _recordAppOpened('resumed');
          } on ApiException catch (e) {
            if (e.statusCode == 401) {
              await _service.logout();
              _user = null;
              _recordAppOpened('login_required');
            } else {
              _user = User.restored;
              _recordAppOpened('resumed');
            }
          } catch (_) {
            _user = User.restored;
            _recordAppOpened('resumed');
          }
        }
      } else {
        _user = null;
        _recordAppOpened('login_required');
      }
    } catch (_) {
      _user = null;
    } finally {
      _isLoading = false;
      _isRestoring = false;
      notifyListeners();
    }
  }

  /// Fires a one-way "app opened" metrics ping (see [init]). Best-effort and
  /// non-blocking: a failure here must never affect the restore flow, so
  /// errors are swallowed rather than surfaced.
  void _recordAppOpened(String sessionState) {
    unawaited(() async {
      try {
        await _service.appOpened(sessionState);
      } catch (_) {}
    }());
  }

  /// Reconciles an optimistically-restored session (see [init]) with the
  /// server without blocking app start. Mirrors the blocking path's
  /// getMe() handling: a 401 forces logout, any other failure (offline,
  /// timeout) leaves the already-restored sentinel session in place.
  Future<void> _refreshMeInBackground() async {
    try {
      final data = await _service.getMe();
      _user = User.fromMap(data);
      await _unlockEncryption();
      notifyListeners();
    } on ApiException catch (e) {
      if (e.statusCode == 401) {
        await _service.logout();
        _user = null;
        notifyListeners();
      }
    } catch (_) {
      // Offline/timeout — keep the optimistically-restored session.
    }
  }

  /// Reads the `exp` claim (seconds since the Unix epoch) out of a JWT's
  /// payload segment, without verifying its signature — the server remains
  /// the source of truth; this is only used to decide whether [init] can
  /// skip blocking on the network. Returns null if [token] isn't a
  /// well-formed JWT or carries no numeric `exp`.
  static DateTime? _jwtExpiry(String token) {
    final parts = token.split('.');
    if (parts.length != 3) return null;
    try {
      final payload = jsonDecode(
        utf8.decode(base64Url.decode(base64Url.normalize(parts[1]))),
      );
      final exp = payload is Map ? payload['exp'] : null;
      if (exp is! num) return null;
      return DateTime.fromMillisecondsSinceEpoch((exp * 1000).round(), isUtc: true);
    } catch (_) {
      return null;
    }
  }

  Future<void> loginWithPassword(String email, String password) async {
    _setLoading(true);
    try {
      final map = await _service.loginWithPassword(email, password);
      _user = User.fromMap(map);
      _error = null;
      await _unlockEncryption();
    } on Exception catch (e) {
      _error = _extractMessage(e);
      _user = null;
    } finally {
      _setLoading(false);
    }
  }

  Future<void> loginWithGoogle(String idToken) async {
    _setLoading(true);
    try {
      final map = await _service.loginWithGoogle(idToken);
      _user = User.fromMap(map);
      _error = null;
      await _unlockEncryption();
    } on Exception catch (e) {
      _error = _extractMessage(e);
      _user = null;
    } finally {
      _setLoading(false);
    }
  }

  /// [email] is the login identifier as well as the address the account is
  /// reachable at (issue #110). [firstName] / [lastName] become the display
  /// name server-side.
  Future<void> register(
    String email,
    String password, {
    required String firstName,
    required String lastName,
  }) async {
    _setLoading(true);
    try {
      final map = await _service.register(
        email,
        password,
        firstName: firstName,
        lastName: lastName,
      );
      _user = User.fromMap(map);
      _error = null;
      await _unlockEncryption();
    } on Exception catch (e) {
      _error = _extractMessage(e);
      _user = null;
    } finally {
      _setLoading(false);
    }
  }

  Future<void> logout() async {
    try {
      await _service.logout();
    } finally {
      // Signed out whatever fails below or above: the router keeps a user it
      // still sees out of /login, so a failed wipe used to leave a dead
      // session on screen — after an account deletion, for one (issue #429).
      encryption.lock();
      _user = null;
      _error = null;
      notifyListeners();
    }
    // Full wipe, not just a rescope: a signed-out device must not keep
    // another account's cached trip data sitting on disk.
    await projectDataCache.clearAll();
    await photoThumbCache.clearAll();
  }

  /// After a successful login/restore, try to unlock encryption on this trusted
  /// device. Harmless no-op when encryption is disabled or this device isn't
  /// approved yet (those fall through to device approval / recovery). Never throws.
  Future<void> _unlockEncryption() async {
    try {
      await encryption.prepareForSession();
    } catch (_) {}
  }

  /// Update the in-memory user from a token-response `user` map.
  void updateUser(Map<String, dynamic> userMap) {
    _user = User.fromMap(userMap);
    notifyListeners();
  }

  /// Re-fetch the profile from the server (e.g. after a forced password change
  /// clears `password_change_required`), so redirects re-evaluate.
  Future<void> refreshProfile() async {
    try {
      _user = User.fromMap(await _service.getMe());
      notifyListeners();
    } catch (_) {}
  }

  /// Consume an emailed verification token (issue #110), then refresh the
  /// profile so the "verify your email" banner clears without a re-login.
  /// Rethrows so the screen can show why a link failed.
  Future<void> verifyEmail(String token) async {
    await _service.verifyEmail(token);
    if (_user != null) await refreshProfile();
  }

  /// Request a fresh verification email. Rethrows [ApiException] so the caller
  /// can distinguish a 429 (too many requests) from a network failure.
  Future<void> resendVerification() => _service.resendVerification();

  void clearError() {
    _error = null;
    notifyListeners();
  }

  // ── helpers ──────────────────────────────────────────────────────────────────

  void _setLoading(bool v) {
    _isLoading = v;
    notifyListeners();
  }

  /// Extracts the FastAPI `"detail"` string from an ApiException body, falling
  /// back to the raw exception message.
  String _extractMessage(Exception e) {
    final s = e.toString();
    final match = RegExp(r'"detail"\s*:\s*"([^"]+)"').firstMatch(s);
    if (match != null) return match.group(1)!;
    return s.replaceFirst('Exception: ', '');
  }
}
