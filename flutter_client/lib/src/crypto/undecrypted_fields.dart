/// Which memory and journal text fields still hold ciphertext this device
/// could not decrypt when they were loaded (issue #466, review R3-1).
///
/// Recorded where the items are revealed, never guessed later from a value's
/// shape: after a save or a reload a field holds the text the user typed, and
/// typed text such as "v1.2.3" looks like an envelope. The memory and journal
/// editors read this to decide which field is shown read-only and saved back
/// exactly as stored.
library;

class UndecryptedFields {
  final _keys = <String>{};

  static String _key(String kind, String id, String field) => '$kind/$id/$field';

  /// Whether [field] of the [kind] item [id] was left undecrypted.
  bool contains(String kind, String? id, String field) =>
      id != null && _keys.contains(_key(kind, id, field));

  void mark(String kind, String id, String field) => _keys.add(_key(kind, id, field));

  /// The user saved [field]: it now holds what they typed, not ciphertext.
  void remove(String kind, String id, String field) =>
      _keys.remove(_key(kind, id, field));

  /// Forget every mark: the items are about to be revealed afresh.
  void reset() => _keys.clear();
}
