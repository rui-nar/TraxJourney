/// Orchestrates the client side of zero-knowledge encryption (issue #26):
/// enabling encryption, and unlocking the Content Master Key (CMK) on a trusted
/// device. Crypto comes from [e2ee_crypto]; device-key persistence and the
/// server API are injected interfaces, so this service is unit-testable with
/// fakes and carries no dart:io / http dependency itself (only
/// `flutter/foundation`, for the [EncryptionService.state] listenable).
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/foundation.dart';

import 'e2ee_crypto.dart';

/// Persists this device's X25519 key pair in the OS keystore. The concrete
/// implementation (flutter_secure_storage / WebCrypto) is a thin I/O adapter
/// supplied by the app; this service never touches storage directly.
abstract class DeviceKeyStore {
  Future<SimpleKeyPair?> load();
  Future<void> save(SimpleKeyPair keyPair);
}

/// Encryption state for the current account + this device, from GET /status.
class EncryptionStatus {
  final bool enabled;
  final List<String> recoveryMethods;
  final bool deviceRegistered;
  final bool deviceApproved;
  final String? wrappedCmkB64;
  final String? ephemeralPublicKeyB64;

  /// Recovery methods whose wrap the user never confirmed having saved
  /// (Decision 16). Only `recovery_key` is ever listed. Empty from a server
  /// that predates the field.
  final List<String> unconfirmedRecoveryMethods;

  const EncryptionStatus({
    required this.enabled,
    required this.recoveryMethods,
    required this.deviceRegistered,
    required this.deviceApproved,
    this.wrappedCmkB64,
    this.ephemeralPublicKeyB64,
    this.unconfirmedRecoveryMethods = const [],
  });
}

/// A device awaiting approval (from GET /devices/pending).
class PendingDevice {
  final String publicKeyB64;
  final String label;
  const PendingDevice(this.publicKeyB64, this.label);
}

/// A stored recovery wrap (from GET /recovery/{method}).
class RecoveryWrapData {
  final String wrappedCmkB64;
  final String saltB64;
  final String? kdfParamsJson;
  const RecoveryWrapData(this.wrappedCmkB64, this.saltB64, this.kdfParamsJson);
}

/// The server endpoints this service needs. Implemented by the app's HTTP layer.
abstract class EncryptionApi {
  Future<EncryptionStatus> fetchStatus(String? devicePublicKeyB64);

  /// Turns encryption on. Resolves to the recovery `wrapped_cmk` the server
  /// says it stored (a base64 [String]); anything else — null from an older
  /// server that does not return it — means "the wrap that was sent". Typed
  /// [Object?] so an implementation answering `Future<void>` still conforms.
  Future<Object?> enable(Map<String, dynamic> payload);
  Future<void> registerDevice(String publicKeyB64, String label);
  Future<List<PendingDevice>> pendingDevices();
  Future<void> approveDevice(
      String publicKeyB64, String wrappedCmkB64, String ephemeralPublicKeyB64);

  /// The recovery wrap for [method], or null if the user didn't configure it.
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method);

  /// Records that the user saved the recovery wrap [wrappedCmkB64] of
  /// [method]. Throws [RecoveryKeyConflict] when that is not the wrap the
  /// server holds (Decision 16, U5b-R1-1); any other failure throws as is.
  Future<void> confirmRecovery(String method, String wrappedCmkB64);

  /// Replaces the unconfirmed `recovery_key` wrap and resolves to the wrap
  /// this request wrote. Throws [RecoveryKeyConflict] when the current key is
  /// already confirmed.
  Future<String> replaceRecoveryKey(String wrappedCmkB64, String saltB64);
}

/// The server refused a recovery-key confirm or replace with a 409: the key on
/// the server is not the one shown, or it is already confirmed.
class RecoveryKeyConflict implements Exception {
  const RecoveryKeyConflict();
  @override
  String toString() => 'the recovery key on the server has changed';
}

/// What the user confirms having saved: the exact recovery wrap they were
/// shown, tied to the session that showed it.
class RecoveryKeyConfirmation {
  final String wrappedCmkB64;
  final int _generation;
  const RecoveryKeyConfirmation._(this.wrappedCmkB64, this._generation);
}

/// How [EncryptionService.confirmRecoveryKey] ended.
enum RecoveryConfirmOutcome {
  /// The server recorded it.
  confirmed,

  /// The key shown is no longer the one on the server (409): discard it.
  conflict,

  /// Not recorded (an older server, the network, an ended session): the key
  /// stays unconfirmed and a replacement is offered at the next sign-in.
  failed,
}

/// A recovery key made by [EncryptionService.replaceRecoveryKey], to show
/// once and then confirm.
class NewRecoveryKey {
  final Uint8List secret;
  final RecoveryKeyConfirmation confirmation;
  const NewRecoveryKey(this.secret, this.confirmation);
}

/// The user's recovery choice at enable-time (the honest A/B decision).
sealed class RecoveryChoice {
  const RecoveryChoice();
}

/// Option A — a high-entropy recovery key the app generates and shows once.
class RecoveryKeyChoice extends RecoveryChoice {
  const RecoveryKeyChoice();
}

/// Medium — security questions; weaker, hardened with Argon2id. Carries the
/// user-selected [questions] (persisted with the wrap — non-secret — so the
/// recovery screen can re-display them) and the parallel [answers] (the wrap key
/// derives from the answers, in order).
class QnaChoice extends RecoveryChoice {
  final List<String> questions;
  final List<String> answers;
  const QnaChoice(this.questions, this.answers);
}

/// High — a user passphrase (Argon2id over the raw passphrase). Recovery-only.
class PassphraseChoice extends RecoveryChoice {
  final String passphrase;
  const PassphraseChoice(this.passphrase);
}

/// Result of enabling encryption. For Option A, [recoverySecret] is the
/// one-time secret the UI must display and then discard (the app never keeps it
/// in plaintext). Null for Option B.
class EnableResult {
  final Uint8List? recoverySecret;

  /// What to confirm once the user saved [recoverySecret]; null without one.
  final RecoveryKeyConfirmation? confirmation;
  const EnableResult(this.recoverySecret, [this.confirmation]);
}

/// Thrown by [EncryptionService.enable] when the session ended (a [lock]
/// — sign-out or a 401) while it was still building the keys: encryption
/// was not turned on (U5-R3-1, issue #418).
class EncryptionSessionEnded implements Exception {
  const EncryptionSessionEnded();
  @override
  String toString() => 'the session ended before encryption was turned on';
}

/// Where this account and device stand with encryption (#506).
enum EncryptionState {
  /// Encryption is off for the account, or its status is not known yet (no
  /// session prepared, or the status request failed). Writes go out as they
  /// are; the server refuses plaintext into an encrypted space on its own.
  disabled,

  /// The key is held in memory: this device reads and writes ciphertext.
  unlocked,

  /// Encryption is on and this device is approved, but the key is not
  /// unwrapped (signed out of it, or the unwrap failed).
  locked,

  /// Encryption is on and this device is not approved yet.
  awaitingApproval,
}

/// What the trip screen and the editors say while [EncryptionState.locked].
const kEncryptionLockedMessage =
    "Encryption is on for your account, but this device hasn't unlocked your "
    "key, so memories and journal entries can't be saved here. Recover "
    'access to unlock it.';

/// What the trip screen and the editors say while
/// [EncryptionState.awaitingApproval].
const kEncryptionAwaitingApprovalMessage =
    'Encryption is on for your account and this device is waiting to be '
    "approved, so memories and journal entries can't be saved here yet. "
    'Approve it from a device you already use, or recover access.';

/// A clear message for the server's encryption refusals (decision 3): a 409
/// whose `detail.code` is `encryption_locked` or `encryption_not_shared`.
/// Null for any other response, which callers report as they did before.
String? encryptionRefusalMessage(int statusCode, String body) {
  if (statusCode != 409) return null;
  Object? code;
  try {
    final decoded = jsonDecode(body);
    final detail = decoded is Map ? decoded['detail'] : null;
    code = detail is Map ? detail['code'] : null;
  } on FormatException {
    return null;
  }
  return switch (code) {
    'encryption_locked' =>
      "This text has to be stored encrypted, and this device can't encrypt "
          'it right now. Unlock encryption on this device (approve it, or '
          'recover access), then try again.',
    'encryption_not_shared' =>
      "This trip isn't encrypted, so it can't store text encrypted with your "
          "key: the other travellers couldn't read it. Retype the text, then "
          'save again.',
    _ => null,
  };
}

class EncryptionService {
  final DeviceKeyStore _store;
  final EncryptionApi _api;
  final String deviceLabel;

  EncryptionService(this._store, this._api, {this.deviceLabel = ''});

  SecretKey? _cmk;

  final _state = ValueNotifier<EncryptionState>(EncryptionState.disabled);

  /// The account's encryption state on this device, kept from the last
  /// status, unlock, recovery or [lock] (#506).
  ValueListenable<EncryptionState> get state => _state;

  /// True once the CMK is held in memory (this device can read/write ciphertext).
  bool get isUnlocked => _cmk != null;

  /// Bumped by every [lock], so an [unlock], [enable] or recovery still in
  /// flight when the session ends cannot put the key back afterwards
  /// (U5-R1-2, U5-R2-1, issue #418). Every assignment of [_cmk] after an
  /// await checks it.
  int _lockGeneration = 0;

  /// The status the last [unlock] read (or a 409 refetched), for
  /// [needsRecoveryKeyReplacement]. Cleared by [lock].
  EncryptionStatus? _status;

  final _changes = StreamController<void>.broadcast();

  /// Fires whenever [needsRecoveryKeyReplacement] may have changed.
  late final Stream<void> changes = _changes.stream;

  /// True while unlocked with a `recovery_key` the user never confirmed
  /// saving (Decision 16): the projects screen offers to replace it.
  bool get needsRecoveryKeyReplacement =>
      isUnlocked &&
      (_status?.unconfirmedRecoveryMethods.contains('recovery_key') ?? false);

  /// Why text that must be encrypted can't be written from this device now,
  /// or null when it can: the account is encrypted and the key is not
  /// unlocked. Text that stays plaintext anyway (a memory on a trip the user
  /// doesn't own) is not concerned; callers decide that.
  String? get writeBlockedMessage => switch (_state.value) {
        EncryptionState.locked => kEncryptionLockedMessage,
        EncryptionState.awaitingApproval => kEncryptionAwaitingApprovalMessage,
        EncryptionState.disabled || EncryptionState.unlocked => null,
      };

  /// Drop the in-memory CMK (e.g. on logout). The account stays encrypted, so
  /// an unlocked device becomes locked; the next [prepareForSession] reads
  /// the next session's state afresh.
  void lock() {
    _cmk = null;
    _status = null;
    _lockGeneration++;
    if (_state.value == EncryptionState.unlocked) {
      _state.value = EncryptionState.locked;
    }
    _changes.add(null);
  }

  /// Enable encryption for the account: generate a CMK, wrap it to this device
  /// and to the chosen recovery method, push the wraps to the server, and hold
  /// the CMK unlocked. Returns the one-time recovery secret for Option A.
  ///
  /// Locked before the request is sent, nothing is sent or saved and
  /// [EncryptionSessionEnded] is thrown: a recovery method nobody was shown
  /// must not be created. Locked while the request itself is out, the server
  /// is enabled but the key is not held; the result, recovery secret
  /// included, goes back to a caller whose session has ended, which may no
  /// longer be there to show it.
  Future<EnableResult> enable(RecoveryChoice choice) async {
    final generation = _lockGeneration;
    final cmk = await generateCmk();

    final keyPair = await _store.load() ?? await generateDeviceKeyPair();
    final devicePub = await keyPair.extractPublicKey();
    final deviceWrap = await wrapCmkToDevicePublicKey(cmk, devicePub);

    final salt = _randomBytes(16);
    Uint8List? recoverySecret;
    final WrappedCmk recoveryWrap;
    final String method;
    String? kdfParamsJson;

    switch (choice) {
      case RecoveryKeyChoice():
        recoverySecret = generateRecoverySecret();
        recoveryWrap = await wrapCmkWithRecoveryKey(cmk, recoverySecret, salt);
        method = 'recovery_key';
      case PassphraseChoice(passphrase: final passphrase):
        const params = Argon2Params();
        recoveryWrap = await wrapCmkWithPassphrase(cmk, passphrase, salt, params);
        method = 'passphrase';
        kdfParamsJson = jsonEncode(params.toJson());
      case QnaChoice(questions: final questions, answers: final answers):
        const params = Argon2Params();
        recoveryWrap = await wrapCmkWithQna(cmk, answers, salt, params);
        method = 'qna';
        kdfParamsJson = jsonEncode({...params.toJson(), 'questions': questions});
    }

    // Every step above is client-side (Argon2 takes seconds on web), so this
    // is where a session that ended meanwhile is caught: before anything is
    // stored or sent. Checked again after the save, which awaits too.
    if (generation != _lockGeneration) throw const EncryptionSessionEnded();
    await _store.save(keyPair);
    if (generation != _lockGeneration) throw const EncryptionSessionEnded();
    final stored = await _api.enable({
      'device': {
        'public_key': base64.encode(devicePub.bytes),
        'label': deviceLabel,
        'wrapped_cmk': base64.encode(deviceWrap.blob),
        'ephemeral_public_key': base64.encode(deviceWrap.ephemeralPublicKey!),
      },
      'recovery': {
        'method': method,
        'wrapped_cmk': base64.encode(recoveryWrap.blob),
        'salt': base64.encode(salt),
        'kdf_params_json': kdfParamsJson,
      },
    });

    if (generation == _lockGeneration) {
      _cmk = cmk;
      _state.value = EncryptionState.unlocked;
    }
    if (recoverySecret == null) return const EnableResult(null);
    // The server's copy when it says what it stored; an older server does not
    // say, and stored what was sent.
    final wrapB64 = stored is String ? stored : base64.encode(recoveryWrap.blob);
    return EnableResult(
        recoverySecret, RecoveryKeyConfirmation._(wrapB64, generation));
  }

  /// Record on the server that the user saved the recovery key [c] came with.
  /// Sends nothing once the session that showed it has ended. A 409 refetches
  /// the status, so [needsRecoveryKeyReplacement] follows the server. Never
  /// throws.
  Future<RecoveryConfirmOutcome> confirmRecoveryKey(
      RecoveryKeyConfirmation c) async {
    if (c._generation != _lockGeneration) return RecoveryConfirmOutcome.failed;
    try {
      await _api.confirmRecovery('recovery_key', c.wrappedCmkB64);
    } on RecoveryKeyConflict {
      await _refreshStatus(c._generation);
      return RecoveryConfirmOutcome.conflict;
    } catch (_) {
      return RecoveryConfirmOutcome.failed;
    }
    final status = _status;
    if (c._generation == _lockGeneration && status != null) {
      _status = EncryptionStatus(
        enabled: status.enabled,
        recoveryMethods: status.recoveryMethods,
        deviceRegistered: status.deviceRegistered,
        deviceApproved: status.deviceApproved,
        wrappedCmkB64: status.wrappedCmkB64,
        ephemeralPublicKeyB64: status.ephemeralPublicKeyB64,
        unconfirmedRecoveryMethods: [
          for (final m in status.unconfirmedRecoveryMethods)
            if (m != 'recovery_key') m,
        ],
      );
      _changes.add(null);
    }
    return RecoveryConfirmOutcome.confirmed;
  }

  /// Replace a recovery key that was never confirmed (Decision 16): generate a
  /// new secret, wrap the CMK under it and send the wrap. Requires
  /// [isUnlocked]. As in [enable], a session that ends before the request is
  /// sent sends nothing and throws [EncryptionSessionEnded]. A 409 (the key
  /// is already confirmed) refetches the status and rethrows
  /// [RecoveryKeyConflict].
  Future<NewRecoveryKey> replaceRecoveryKey() async {
    final generation = _lockGeneration;
    final cmk = _requireCmk();
    final secret = generateRecoverySecret();
    final salt = _randomBytes(16);
    final wrap = await wrapCmkWithRecoveryKey(cmk, secret, salt);
    if (generation != _lockGeneration) throw const EncryptionSessionEnded();
    final String written;
    try {
      written = await _api.replaceRecoveryKey(
          base64.encode(wrap.blob), base64.encode(salt));
    } on RecoveryKeyConflict {
      await _refreshStatus(generation);
      rethrow;
    }
    return NewRecoveryKey(
        secret, RecoveryKeyConfirmation._(written, generation));
  }

  /// Re-read the status for [needsRecoveryKeyReplacement], unless the session
  /// [generation] belongs to has ended. Never throws.
  Future<void> _refreshStatus(int generation) async {
    try {
      final keyPair = await _store.load();
      final pubB64 = keyPair == null
          ? null
          : base64.encode((await keyPair.extractPublicKey()).bytes);
      final status = await _api.fetchStatus(pubB64);
      if (generation != _lockGeneration) return;
      _status = status;
      _changes.add(null);
    } catch (_) {}
  }

  /// Try to unlock on a trusted device: load the stored key pair, ask the server
  /// for this device's wrapped CMK, and unwrap it. Returns false if there is no
  /// stored key, encryption is off, or this device is not yet approved — those
  /// cases fall through to device-approval (Phase 5) or recovery (Phase 6).
  Future<bool> unlock() async {
    final generation = _lockGeneration;
    final keyPair = await _store.load();
    if (keyPair == null) return false;

    final pub = await keyPair.extractPublicKey();
    final status = await _api.fetchStatus(base64.encode(pub.bytes));
    if (!status.enabled) {
      _state.value = EncryptionState.disabled;
      return false;
    }
    if (!status.deviceApproved || status.wrappedCmkB64 == null) {
      _state.value = EncryptionState.awaitingApproval;
      return false;
    }

    final wrapped = WrappedCmk(
      base64.decode(status.wrappedCmkB64!),
      ephemeralPublicKey: base64.decode(status.ephemeralPublicKeyB64!),
    );
    final SecretKey cmk;
    try {
      cmk = await unwrapCmkWithDeviceKeyPair(wrapped, keyPair);
    } catch (_) {
      // Approved, yet the key would not unwrap: encrypted and locked, not
      // unknown — writes that need the key must stop.
      if (generation == _lockGeneration) _state.value = EncryptionState.locked;
      rethrow;
    }
    // Locked while this was waiting — a logout, or a 401 ending the session.
    if (generation != _lockGeneration) return false;
    _cmk = cmk;
    _status = status;
    _state.value = EncryptionState.unlocked;
    _changes.add(null);
    return true;
  }

  /// Prepare encryption for a freshly-authenticated session: unlock on a trusted
  /// device; or, if encryption is enabled but this device isn't approved yet,
  /// register it as pending so a trusted device can approve it. Returns whether
  /// the CMK is now unlocked. Sets [state] from the status it reads; when the
  /// status can't be read it stays [EncryptionState.disabled] (unknown), and
  /// the server's refusals stand in for the client's gate.
  Future<bool> prepareForSession() async {
    if (!isUnlocked) _state.value = EncryptionState.disabled;
    if (await unlock()) return true;
    final keyPair = await _store.load();
    final pubB64 = keyPair == null
        ? null
        : base64.encode((await keyPair.extractPublicKey()).bytes);
    final status = await _api.fetchStatus(pubB64);
    if (!status.enabled) {
      _state.value = EncryptionState.disabled;
    } else if (!status.deviceApproved) {
      _state.value = EncryptionState.awaitingApproval;
      await registerThisDevice();
    } else {
      _state.value = EncryptionState.locked;
    }
    return false;
  }

  // ── Cross-device approval (trusted-device model) ──────────────────────────

  /// Register THIS device for approval: ensure a device key pair exists locally,
  /// then post its public key as pending. A trusted device approves it later;
  /// afterwards [unlock] succeeds. Call when [unlock] returned false because the
  /// device isn't registered/approved yet.
  Future<void> registerThisDevice({String label = ''}) async {
    final keyPair = await _store.load() ?? await generateDeviceKeyPair();
    await _store.save(keyPair);
    final pub = await keyPair.extractPublicKey();
    await _api.registerDevice(
        base64.encode(pub.bytes), label.isEmpty ? deviceLabel : label);
  }

  /// Devices awaiting approval on this account (shown on a trusted device).
  Future<List<PendingDevice>> pendingDevices() => _api.pendingDevices();

  /// Approve a pending device by wrapping the CMK to its public key. Requires an
  /// unlocked CMK — which only a trusted device has, so only it can approve.
  Future<void> approveDevice(String devicePublicKeyB64) async {
    final cmk = _requireCmk();
    final pub = SimplePublicKey(base64.decode(devicePublicKeyB64),
        type: KeyPairType.x25519);
    final wrapped = await wrapCmkToDevicePublicKey(cmk, pub);
    await _api.approveDevice(
      devicePublicKeyB64,
      base64.encode(wrapped.blob),
      base64.encode(wrapped.ephemeralPublicKey!),
    );
  }

  // ── Recovery (no trusted device available) ────────────────────────────────

  /// Unlock with a generated recovery key (High / Option A), then re-trust this
  /// device. Returns false if the method isn't configured or the key is wrong.
  Future<bool> recoverWithRecoveryKey(List<int> recoverySecret) =>
      _recover('recovery_key', (w) => unwrapCmkWithRecoveryKey(
            WrappedCmk(base64.decode(w.wrappedCmkB64)),
            recoverySecret, base64.decode(w.saltB64),
          ));

  /// Unlock with the user's passphrase (High), then re-trust this device.
  Future<bool> recoverWithPassphrase(String passphrase) =>
      _recover('passphrase', (w) => unwrapCmkWithPassphrase(
            WrappedCmk(base64.decode(w.wrappedCmkB64)),
            passphrase, base64.decode(w.saltB64), _argonFrom(w.kdfParamsJson),
          ));

  /// Unlock by answering the security questions (Medium), then re-trust device.
  Future<bool> recoverWithQna(List<String> answers) =>
      _recover('qna', (w) => unwrapCmkWithQna(
            WrappedCmk(base64.decode(w.wrappedCmkB64)),
            answers, base64.decode(w.saltB64), _argonFrom(w.kdfParamsJson),
          ));

  /// The security questions the user chose at enable, so the recovery screen can
  /// re-display them. Empty if Q&A recovery isn't configured.
  Future<List<String>> qnaRecoveryQuestions() async {
    final json = (await _api.fetchRecoveryWrap('qna'))?.kdfParamsJson;
    if (json == null) return const [];
    final decoded = jsonDecode(json);
    final q = decoded is Map<String, dynamic> ? decoded['questions'] : null;
    return q is List ? q.cast<String>() : const [];
  }

  Future<bool> _recover(
      String method, Future<SecretKey> Function(RecoveryWrapData) unwrap) async {
    final generation = _lockGeneration;
    final wrap = await _api.fetchRecoveryWrap(method);
    if (wrap == null) return false;
    final SecretKey cmk;
    try {
      cmk = await unwrap(wrap);
    } catch (_) {
      return false; // wrong secret / corrupt wrap
    }
    // Locked meanwhile: the session ended, so neither hold the key nor
    // re-trust this device with it.
    if (generation != _lockGeneration) return false;
    _cmk = cmk;
    _state.value = EncryptionState.unlocked;
    await _retrustThisDevice();
    return true;
  }

  /// After recovery this device holds the CMK; register + wrap the CMK to its own
  /// key so future sessions unlock via the device (no recovery needed again).
  Future<void> _retrustThisDevice() async {
    final keyPair = await _store.load() ?? await generateDeviceKeyPair();
    await _store.save(keyPair);
    final pubB64 = base64.encode((await keyPair.extractPublicKey()).bytes);
    await _api.registerDevice(pubB64, deviceLabel);
    await approveDevice(pubB64);
  }

  Argon2Params _argonFrom(String? json) {
    if (json == null) return const Argon2Params();
    return Argon2Params.fromJson(jsonDecode(json) as Map<String, dynamic>);
  }

  /// Encrypt a text field to a stored envelope. Requires [isUnlocked].
  Future<String> encryptText(String plaintext) async {
    final enc = await encryptField(plaintext, _requireCmk());
    return enc.encode();
  }

  /// Decrypt a stored envelope. Requires [isUnlocked]; throws on wrong key/tamper.
  Future<String> decryptText(String envelope) =>
      decryptField(EncryptedField.decode(envelope), _requireCmk());

  // ── CRUD-boundary helpers (used by the notifiers) ─────────────────────────

  /// Encrypt a field for storage when encryption is unlocked; otherwise pass it
  /// through unchanged (encryption off, or locked → store plaintext as before).
  Future<String?> protect(String? plaintext) async {
    if (plaintext == null || plaintext.isEmpty || !isUnlocked) return plaintext;
    return encryptText(plaintext);
  }

  /// Reveal a stored field: decrypt when it's an encrypted envelope and we're
  /// unlocked; otherwise return it unchanged (plaintext, or still-locked
  /// ciphertext the caller renders as locked). Never throws.
  Future<String?> reveal(String? value) async {
    if (value == null || !EncryptedField.isEnvelope(value) || !isUnlocked) {
      return value;
    }
    try {
      return await decryptText(value);
    } catch (_) {
      return value; // wrong key / corrupt — surface as-is rather than crash
    }
  }

  SecretKey _requireCmk() {
    final cmk = _cmk;
    if (cmk == null) {
      throw StateError('Encryption is locked — unlock the CMK first');
    }
    return cmk;
  }

  Uint8List _randomBytes(int n) {
    final rnd = Random.secure();
    return Uint8List.fromList(List<int>.generate(n, (_) => rnd.nextInt(256)));
  }
}
