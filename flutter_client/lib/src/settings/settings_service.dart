library;

import '../api/client.dart';

class SettingsService {
  Future<Map<String, dynamic>> getProfile() async =>
      await api.get('/api/auth/me') as Map<String, dynamic>;

  Future<({String? token, Map<String, dynamic> user})> updateProfile(
      String displayName) async {
    try {
      final data = await api.put('/api/auth/me', {'display_name': displayName})
          as Map<String, dynamic>;
      return (
        token: data['access_token'] as String?,
        user: data['user'] as Map<String, dynamic>? ?? {},
      );
    } on ApiException catch (e) {
      throw Exception(_detail(e.body));
    }
  }

  Future<({String? token, Map<String, dynamic> user})> changePassword({
    required String current,
    required String next,
  }) async {
    try {
      final data = await api.post('/api/auth/change-password', {
        'current_password': current,
        'new_password': next,
      }) as Map<String, dynamic>;
      return (
        token: data['access_token'] as String?,
        user: data['user'] as Map<String, dynamic>? ?? {},
      );
    } on ApiException catch (e) {
      throw Exception(_detail(e.body));
    }
  }

  Future<void> deleteAccount() async {
    try {
      await api.delete('/api/auth/me');
    } on ApiException catch (e) {
      throw Exception(_detail(e.body));
    }
  }

  Future<bool> getStravaStatus() async {
    final data = await api.get('/api/strava/status') as Map<String, dynamic>;
    return data['connected'] == true;
  }

  Future<String> getStravaConnectUrl() async {
    final data = await api.get('/api/strava/connect') as Map<String, dynamic>;
    return data['url'] as String;
  }

  Future<void> disconnectStrava() async => api.delete('/api/strava/disconnect');

  Future<Map<String, dynamic>> getPolarstepsStatus() async =>
      await api.get('/api/polarsteps/status') as Map<String, dynamic>;

  Future<Map<String, dynamic>> connectPolarsteps(String token) async =>
      await api.post('/api/polarsteps/connect', {'remember_token': token})
          as Map<String, dynamic>;

  Future<void> disconnectPolarsteps() async =>
      api.delete('/api/polarsteps/disconnect');

  Future<Map<String, dynamic>> getImmichStatus() async =>
      await api.get('/api/immich/status') as Map<String, dynamic>;

  Future<void> saveImmichConfig({
    required String serverUrl,
    required String apiKey,
  }) async {
    try {
      await api.put('/api/immich/config', {
        'server_url': serverUrl,
        'api_key': apiKey,
      });
    } on ApiException catch (e) {
      throw Exception(_detail(e.body));
    }
  }

  Future<void> disconnectImmich() async =>
      api.delete('/api/immich/disconnect');

  /// Backups are admin-only. A 403 (e.g. admin rights revoked since sign-in)
  /// yields an empty list rather than an error.
  Future<List<Map<String, dynamic>>> listBackups() async {
    try {
      final data = await api.get('/api/backup/') as List<dynamic>;
      return data.cast<Map<String, dynamic>>();
    } on ApiException catch (e) {
      if (e.statusCode == 403) return const [];
      rethrow;
    }
  }

  Future<void> restoreBackup(String date) async {
    try {
      await api.post('/api/backup/$date/restore', {});
    } on ApiException catch (e) {
      throw Exception(_detail(e.body));
    }
  }

  static String _detail(String body) => apiErrorDetail(body);
}
