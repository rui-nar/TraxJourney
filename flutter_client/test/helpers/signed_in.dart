/// A signed-in session for tests that exercise per-account behaviour.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:traxjourney_client/src/api/client.dart';

/// An unsigned JWT whose payload carries [sub], the way the server's
/// `create_access_token` names the account. Nothing client-side verifies the
/// signature, so a dummy one is fine.
String fakeJwt({required int sub}) {
  String seg(Object payload) =>
      base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
  return '${seg({'alg': 'none', 'typ': 'JWT'})}.${seg({'sub': '$sub'})}.sig';
}

/// Replaces the global [api] with one signed in as account [userId], talking
/// to [httpClient] when given.
void signInAs(int userId, {http.Client? httpClient}) {
  api = ApiClient(httpClient: httpClient)..setToken(fakeJwt(sub: userId));
}
