/// Base HTTP client for the TraxJourney REST API.
///
/// Stores the JWT returned by /api/auth/token or /api/auth/google
/// and attaches it as a Bearer token on every request.
library;

import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

// Empty string → relative URLs (same origin, production).
// Override at build time: --dart-define=API_BASE_URL=http://localhost:8000
const _kApiBaseUrl = String.fromEnvironment('API_BASE_URL');

class ApiClient {
  final String baseUrl;
  final http.Client _client;
  String? _token;

  // httpClient is injectable so tests can supply a MockClient
  // (package:http/testing.dart); production uses the default.
  ApiClient({this.baseUrl = _kApiBaseUrl, http.Client? httpClient})
      : _client = httpClient ?? http.Client();

  void setToken(String token) => _token = token;
  void clearToken() => _token = null;
  bool get isAuthenticated => _token != null;

  /// Exposes the current JWT for multipart upload requests built outside
  /// this class (e.g. ProjectsNotifier on web where dart:io is unavailable).
  String? get tokenForUpload => _token;

  /// The signed-in account's user id, from the token's `sub` claim, or null
  /// with no token or one that does not parse. Not verified — the server does
  /// that on every request; this only says whose session the device holds.
  ///
  /// Read from the token rather than the profile because it is there the
  /// moment a session is restored, offline included, while the profile's id
  /// is not: a restored session starts as `User.restored` (id ''), and
  /// /api/auth/me echoes the JWT payload, which has no `id` claim either.
  int? get tokenUserId {
    final parts = _token?.split('.');
    if (parts == null || parts.length != 3) return null;
    try {
      final payload = jsonDecode(
          utf8.decode(base64Url.decode(base64Url.normalize(parts[1]))));
      final sub = payload is Map ? payload['sub'] : null;
      return sub == null ? null : int.tryParse(sub.toString());
    } catch (_) {
      return null;
    }
  }

  Map<String, String> get _headers => {
    'Content-Type': 'application/json',
    if (_token != null) 'Authorization': 'Bearer $_token',
  };

  static const _kDefaultTimeout = Duration(seconds: 30);

  Future<dynamic> get(String path, {Duration timeout = _kDefaultTimeout}) async {
    final res = await _client
        .get(Uri.parse('$baseUrl$path'), headers: _headers)
        .timeout(timeout);
    return _handle(res);
  }

  /// GET a JSON endpoint and return its **undecoded** UTF-8 bytes.
  ///
  /// The counterpart to [get] for payloads big enough that decoding them on
  /// the calling isolate drops frames — the full-res geo and the ~12 MB
  /// project details (issue #292). Callers hand the bytes to
  /// `heavy_decode.dart`, which parses them on a worker isolate.
  ///
  /// Error bodies deliberately stay on the string path: they are small, and
  /// [ApiException] carries the text.
  Future<Uint8List> getBytes(String path,
      {Duration timeout = _kDefaultTimeout}) async {
    final res = await _client
        .get(Uri.parse('$baseUrl$path'), headers: _headers)
        .timeout(timeout);
    if (res.statusCode >= 200 && res.statusCode < 300) return res.bodyBytes;
    throw ApiException(res.statusCode, res.body,
        location: res.headers['location']);
  }

  Future<dynamic> post(
    String path,
    Map<String, dynamic> body, {
    Duration timeout = _kDefaultTimeout,
  }) async {
    final res = await _client
        .post(
          Uri.parse('$baseUrl$path'),
          headers: _headers,
          body: jsonEncode(body),
        )
        .timeout(timeout);
    return _handle(res);
  }

  Future<dynamic> put(String path, Map<String, dynamic> body, {Duration timeout = _kDefaultTimeout}) async {
    final res = await _client
        .put(
          Uri.parse('$baseUrl$path'),
          headers: _headers,
          body: jsonEncode(body),
        )
        .timeout(timeout);
    return _handle(res);
  }

  Future<dynamic> delete(String path, {Duration timeout = _kDefaultTimeout}) async {
    final res = await _client
        .delete(Uri.parse('$baseUrl$path'), headers: _headers)
        .timeout(timeout);
    return _handle(res);
  }

  /// Binary downloads (e.g. full-res photos) can legitimately take longer
  /// than the 30s JSON default, so [getRaw] uses the same generous budget as
  /// the other heavy project endpoints (see project_service.dart's
  /// _kLoadTimeout) instead of hanging indefinitely.
  static const _kRawTimeout = Duration(seconds: 60);

  /// Fetch a binary resource and return the raw response.
  /// Use this for file downloads where the body is not JSON.
  Future<http.Response> getRaw(String path, {Duration timeout = _kRawTimeout}) async {
    final res = await _client
        .get(Uri.parse('$baseUrl$path'), headers: _headers)
        .timeout(timeout);
    if (res.statusCode >= 200 && res.statusCode < 300) return res;
    throw ApiException(res.statusCode, res.body);
  }

  /// POST a JSON body and return the raw response (binary, e.g. an image) —
  /// the POST counterpart to [getRaw] for endpoints that return bytes rather
  /// than JSON (e.g. the poster layout preview).
  Future<http.Response> postRaw(
    String path,
    Map<String, dynamic> body, {
    Duration timeout = _kDefaultTimeout,
  }) async {
    final res = await _client
        .post(
          Uri.parse('$baseUrl$path'),
          headers: _headers,
          body: jsonEncode(body),
        )
        .timeout(timeout);
    if (res.statusCode >= 200 && res.statusCode < 300) return res;
    throw ApiException(res.statusCode, res.body);
  }

  dynamic _handle(http.Response res) {
    if (res.statusCode >= 200 && res.statusCode < 300) {
      if (res.body.isEmpty) return null;
      return jsonDecode(res.body);
    }
    throw ApiException(res.statusCode, res.body,
        location: res.headers['location']);
  }
}

class ApiException implements Exception {
  final int statusCode;
  final String body;

  /// The `Location` header, when the server responded with a redirect
  /// (3xx). Redirects reach here as errors rather than being followed
  /// transparently to a JSON response, which usually means the configured
  /// server URL uses the wrong scheme (http vs https) or host.
  final String? location;

  ApiException(this.statusCode, this.body, {this.location});

  @override
  String toString() {
    if (statusCode >= 300 && statusCode < 400 && location != null) {
      return 'ApiException($statusCode): server redirected to $location — '
          'check your server URL is correct (e.g. http vs https)';
    }
    return 'ApiException($statusCode): $body';
  }
}

/// Singleton instance shared across the app. Mutable (not `final`) so
/// main() can swap in a self-hosted server override, read from
/// core/server_config.dart, once at startup before runApp — every call
/// site just reads `api` dynamically, so reassigning it there is enough.
ApiClient api = ApiClient();
