/// Admin dashboard service — wraps /api/admin/* endpoints.
library;

import '../api/client.dart';

class AdminService {
  /// Dashboard metrics: `{totals: {...}, users: [ {...} ]}`.
  Future<Map<String, dynamic>> getStats() async =>
      await api.get('/api/admin/stats') as Map<String, dynamic>;

  /// Force a storage-cache recompute; the next getStats re-walks the filesystem.
  Future<void> refreshStorage() async =>
      await api.get('/api/admin/storage/refresh');

  /// Search users by email / username / display name (case-insensitive).
  Future<List<Map<String, dynamic>>> searchUsers(String query,
      {int limit = 50}) async {
    final q = Uri.encodeQueryComponent(query);
    final data = await api.get('/api/admin/users/search?q=$q&limit=$limit')
        as List<dynamic>;
    return data.cast<Map<String, dynamic>>();
  }

  /// Reset a user's password. Returns the one-time temp password on success.
  /// Throws [ApiException] (409) when the account's encryption tier forbids it.
  Future<String> resetPassword(int userInfoId) async {
    final data = await api.post(
        '/api/admin/users/$userInfoId/reset-password', {}) as Map<String, dynamic>;
    return data['temp_password'] as String;
  }

  /// Grant or revoke admin access for a user.
  /// Throws [ApiException] (409) when revoking your own admin access.
  Future<void> setAdmin(int userInfoId, bool isAdmin) async {
    await api.post('/api/admin/users/$userInfoId/set-admin', {'is_admin': isAdmin});
  }

  /// Permanently delete a user and all their data.
  /// Throws an [Exception] carrying the server's message when the deletion is
  /// refused — deleting your own account this way (409), or a paid plan that
  /// could not be cancelled first (502/409, issue #429) — rather than the raw
  /// response body.
  Future<void> deleteUser(int userInfoId) async {
    try {
      await api.delete('/api/admin/users/$userInfoId');
    } on ApiException catch (e) {
      throw Exception(apiErrorDetail(e.body));
    }
  }

  /// Send a plain-text email to the given users, or to every user when
  /// [sendToAll] is true (in which case [userIds] is ignored). Returns the
  /// number of recipients the email was queued for.
  Future<int> broadcastEmail({
    required String subject,
    required String body,
    List<int> userIds = const [],
    bool sendToAll = false,
  }) async {
    final data = await api.post('/api/admin/broadcast-email', {
      'subject': subject,
      'body': body,
      'user_ids': userIds,
      'send_to_all': sendToAll,
    }) as Map<String, dynamic>;
    return data['sent_count'] as int;
  }

  /// Current effective log level: `{effective_level, source, env_level,
  /// override_level, override_expires_at}`.
  Future<Map<String, dynamic>> getLogLevel() async =>
      await api.get('/api/admin/log-level') as Map<String, dynamic>;

  /// Apply a live log-level override (no restart required). [durationMinutes]
  /// null applies it indefinitely — until manually reverted or a restart.
  /// Throws [ApiException] (422) for an unknown level or an out-of-range
  /// duration.
  Future<Map<String, dynamic>> setLogLevel(String level,
      {int? durationMinutes}) async =>
      await api.put('/api/admin/log-level', {
        'level': level,
        'duration_minutes': durationMinutes,
      }) as Map<String, dynamic>;

  /// Revert to the env-configured (`LOG_LEVEL`) baseline.
  Future<Map<String, dynamic>> clearLogLevelOverride() async =>
      await api.delete('/api/admin/log-level') as Map<String, dynamic>;
}
