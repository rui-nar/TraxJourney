/// Strava connect bound to the client that started it
/// (docs/STRAVA_CONNECT_BINDING_PLAN.md, D2 and D7).
///
/// [StravaConnectFlow.start] makes a fresh PKCE-style verifier, keeps it as
/// the one pending connect and sends only its SHA-256 challenge to the
/// server. [StravaConnectFlow.complete] sends the verifier back with the code
/// and state Strava relayed, and the server links the account only if both
/// match. The verifier never leaves this client otherwise, and is never logged.
///
/// Web keeps the pending connect in memory: the page that opened the popup
/// stays alive. The app keeps it in secure storage, because Android may kill
/// the app while the browser is in front.
library;

import 'dart:convert';
import 'dart:math';

import 'package:cryptography_plus/cryptography_plus.dart';

import '../api/client.dart';
import '../crypto/device_key_store.dart';
import 'settings_service.dart';

/// How a connect attempt ended.
enum StravaConnectOutcome {
  connected,

  /// No verifier is pending here, or it is older than [StravaConnectFlow.ttl].
  noPendingConnect,

  /// The signed state expired before the flow came back.
  expired,

  /// The state belongs to another account, or the verifier does not match it.
  wrongAccount,

  /// The user declined on Strava's consent screen.
  denied,

  failed,

  /// The server retired the call this client made (426).
  updateRequired,
}

/// Thrown by [StravaConnectFlow.start] when the server says the client must
/// be updated. Other failures are rethrown as they came.
class StravaConnectException implements Exception {
  final StravaConnectOutcome outcome;
  const StravaConnectException(this.outcome);

  @override
  String toString() => stravaConnectMessage(outcome);
}

/// Maps a fixed reason token relayed by the OAuth callback to an outcome.
StravaConnectOutcome stravaOutcomeForReason(String? reason) =>
    switch (reason) {
      'denied' => StravaConnectOutcome.denied,
      'state_expired' => StravaConnectOutcome.expired,
      'update_required' => StravaConnectOutcome.updateRequired,
      _ => StravaConnectOutcome.failed,
    };

/// The message shown to the user for [outcome].
String stravaConnectMessage(StravaConnectOutcome outcome) => switch (outcome) {
      StravaConnectOutcome.connected => 'Strava connected!',
      StravaConnectOutcome.noPendingConnect =>
        'This Strava connection was not started here, or it took too long. '
            'Press Connect Strava again.',
      StravaConnectOutcome.expired =>
        'The Strava connection took too long. Press Connect Strava again.',
      StravaConnectOutcome.wrongAccount =>
        'This Strava connection was started from another account or device, '
            'so it was not linked.',
      StravaConnectOutcome.denied => 'Strava access was not granted.',
      StravaConnectOutcome.failed =>
        'Strava connection failed. Please try again.',
      StravaConnectOutcome.updateRequired =>
        'Update the app to connect Strava.',
    };

class StravaConnectFlow {
  /// Matches the server's state lifetime.
  static const ttl = Duration(minutes: 10);
  static const _storeKey = 'strava_connect_pending_v1';

  final SettingsService _api;
  final SecureKvStore _store;
  final DateTime Function() _now;
  final Random _random;

  /// Web only: the pending connect, kept in memory.
  ({String verifier, DateTime expiresAt})? _memory;

  StravaConnectFlow({
    SettingsService? api,
    SecureKvStore? store,
    DateTime Function()? now,
    Random? random,
  })  : _api = api ?? SettingsService(),
        _store = store ?? FlutterSecureKvStore(),
        _now = now ?? DateTime.now,
        _random = random ?? Random.secure();

  /// base64url(sha256(verifier)), no padding (RFC 7636 S256).
  static Future<String> challengeFor(String verifier) async {
    final hash = await Sha256().hash(ascii.encode(verifier));
    return _b64url(hash.bytes);
  }

  /// Starts a connect and returns the Strava authorize URL. [app] chooses
  /// where the server sends the user back: the custom scheme or the web
  /// popup page. Replaces any pending connect.
  Future<Uri> start({required bool app}) async {
    final verifier =
        _b64url(List<int>.generate(32, (_) => _random.nextInt(256)));
    final expiresAt = _now().add(ttl);
    if (app) {
      _memory = null;
      await _store.write(
          _storeKey,
          jsonEncode({
            'verifier': verifier,
            'expires_at': expiresAt.millisecondsSinceEpoch,
          }));
    } else {
      _memory = (verifier: verifier, expiresAt: expiresAt);
    }
    try {
      final url = await _api.startStravaConnect(
          await challengeFor(verifier), app ? 'app' : 'web');
      return Uri.parse(url);
    } on ApiException catch (e) {
      if (e.statusCode == 426) {
        throw const StravaConnectException(
            StravaConnectOutcome.updateRequired);
      }
      rethrow;
    }
  }

  /// Finishes the pending connect with the [code] and [state] Strava
  /// relayed. Clears the pending connect whatever the outcome.
  Future<StravaConnectOutcome> complete(String code, String state) async {
    final inMemory = _memory;
    _memory = null;
    final pending = inMemory ?? await _readStored();
    if (inMemory == null) await _store.delete(_storeKey);
    if (pending == null || !_now().isBefore(pending.expiresAt)) {
      return StravaConnectOutcome.noPendingConnect;
    }
    try {
      await _api.completeStravaConnect(
          code: code, state: state, verifier: pending.verifier);
      return StravaConnectOutcome.connected;
    } on ApiException catch (e) {
      if (e.statusCode == 403) return StravaConnectOutcome.wrongAccount;
      if (e.statusCode == 400 && apiErrorDetail(e.body) == 'state_expired') {
        return StravaConnectOutcome.expired;
      }
      return StravaConnectOutcome.failed;
    } catch (_) {
      return StravaConnectOutcome.failed;
    }
  }

  Future<({String verifier, DateTime expiresAt})?> _readStored() async {
    final raw = await _store.read(_storeKey);
    if (raw == null) return null;
    try {
      final m = jsonDecode(raw) as Map<String, dynamic>;
      return (
        verifier: m['verifier'] as String,
        expiresAt:
            DateTime.fromMillisecondsSinceEpoch(m['expires_at'] as int),
      );
    } catch (_) {
      return null; // Unreadable: treat as no pending connect.
    }
  }

  static String _b64url(List<int> bytes) =>
      base64Url.encode(bytes).replaceAll('=', '');
}
