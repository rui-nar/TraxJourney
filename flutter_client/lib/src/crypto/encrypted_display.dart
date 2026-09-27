/// What the app shows for a field it could not decrypt (issue #466).
///
/// `ProjectNotifier` decrypts encrypted fields in place when the device is
/// unlocked (`EncryptionService.reveal`). A field it could not decrypt keeps
/// its ciphertext envelope: the device is locked, or the key is not this
/// account's (a trip imported from another account's export). The envelope
/// stays in the data, so a save sends it back unchanged and never replaces
/// the ciphertext with this label; only what is displayed changes.
library;

import 'e2ee_crypto.dart' show EncryptedField;

/// Shown in place of a field the app cannot decrypt.
const kEncryptedUnavailable = 'Encrypted content unavailable';

/// True when [value] is still a ciphertext envelope.
bool isUndecrypted(Object? value) =>
    value is String && EncryptedField.isEnvelope(value);

/// [value] for display: [kEncryptedUnavailable] when it is still a
/// ciphertext envelope, else unchanged.
String? shownText(String? value) =>
    isUndecrypted(value) ? kEncryptedUnavailable : value;

/// [value] where it would be sent on or used as text (a poster, a social
/// post): null when it is still a ciphertext envelope, else unchanged.
String? readableOrNull(String? value) => isUndecrypted(value) ? null : value;
