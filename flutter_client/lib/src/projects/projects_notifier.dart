import 'dart:convert';

import 'package:flutter/foundation.dart' show ChangeNotifier;
import 'package:file_picker/file_picker.dart';
import 'package:http/http.dart' as http;

import '../api/client.dart';
import '../billing/billing_service.dart';
import 'project_file.dart';
import 'projects_service.dart';

/// What to do when an imported trip's name is already taken (issue #452).
enum ImportConflictChoice {
  /// Import as a new trip under the first free `<name> (n)`.
  keepBoth('copy'),

  /// Overwrite the existing trip's content with the file's.
  replace('replace');

  const ImportConflictChoice(this.queryValue);

  /// The server's `on_conflict` value.
  final String queryValue;
}

class ProjectsNotifier extends ChangeNotifier {
  final ProjectsService _service;

  List<Map<String, dynamic>> _projects = [];
  bool _isLoading = false;
  String? _error;
  QuotaError? _quotaError;
  String? _nameConflict;

  ProjectsNotifier(this._service);

  List<Map<String, dynamic>> get projects => List.unmodifiable(_projects);
  bool get isLoading => _isLoading;
  String? get error => _error;

  /// Set when the last action was refused by a plan limit (402, issue #121).
  /// The screen turns this into an upgrade prompt instead of an error message.
  QuotaError? get quotaError => _quotaError;

  void clearQuotaError() {
    _quotaError = null;
  }

  /// The trip name the last import was refused for because the user already
  /// has a trip called that (409 `name_conflict`, issue #452). Not an error:
  /// the screen asks whether to keep both or replace, and imports again.
  String? get nameConflict => _nameConflict;

  void clearNameConflict() {
    _nameConflict = null;
  }

  /// Called by [ChangeNotifierProxyProvider] whenever [AuthNotifier] changes.
  void onAuthChanged(bool isLoggedIn) {
    if (isLoggedIn) {
      load();
    } else {
      _projects = [];
      _error = null;
      notifyListeners();
    }
  }

  Future<void> load() async {
    _isLoading = true;
    _error = null;
    notifyListeners();
    try {
      _projects = await _service.list();
    } on Exception catch (e) {
      _error = _msg(e);
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  Future<void> delete(String name) async {
    _isLoading = true;
    _error = null;
    notifyListeners();
    try {
      await api.delete('/api/projects/${Uri.encodeComponent(name)}');
      _projects = _projects.where((p) => p['name'] != name).toList();
    } on Exception catch (e) {
      _error = _msg(e);
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  Future<void> create(String name) async {
    if (name.trim().isEmpty) return;
    _isLoading = true;
    _error = null;
    _quotaError = null;
    notifyListeners();
    try {
      final project = await _service.create(name.trim());
      _projects = [..._projects, project];
    } on Exception catch (e) {
      _quotaError = _quota(e);
      _error = _msg(e);
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  /// Step 1 of import: open file picker and return the bytes + suggested name.
  /// Returns null if the user cancels or on error (sets [error] on failure).
  Future<({List<int> bytes, String defaultName})?> pickProjectFile() async {
    _error = null;
    notifyListeners();
    try {
      final picked = await FilePicker.pickFile(
        type: FileType.custom,
        allowedExtensions: [kProjectFileExtension],
      );
      if (picked == null) return null;
      final rawName = picked.name;
      const suffix = '.$kProjectFileExtension';
      // The extension filter is only a hint on web ("All files" bypasses it)
      // and the upload always adds .traxj, so an older-format project file would
      // otherwise import as a project named after its old suffix.
      if (!rawName.toLowerCase().endsWith(suffix) ||
          rawName.length == suffix.length) {
        _error = 'Choose a $suffix project file.';
        notifyListeners();
        return null;
      }
      final bytes = await picked.readAsBytes();
      final defaultName = rawName.substring(0, rawName.length - suffix.length);
      return (bytes: bytes, defaultName: defaultName);
    } on Exception catch (e) {
      _error = _msg(e);
      notifyListeners();
      return null;
    }
  }

  /// Step 2 of import: upload [bytes] as project [name].
  /// Returns the saved project name on success, null on failure — or when the
  /// name is taken and no [onConflict] was given, in which case
  /// [nameConflict] holds it.
  Future<String?> uploadProjectFile({
    required List<int> bytes,
    required String name,
    ImportConflictChoice? onConflict,
  }) async {
    _isLoading = true;
    _error = null;
    _quotaError = null;
    _nameConflict = null;
    notifyListeners();
    try {
      final data = await _uploadBytes(
          bytes: bytes,
          filename: '$name.$kProjectFileExtension',
          onConflict: onConflict);
      await load();
      return data['name'] as String?;
    } on Exception catch (e) {
      _nameConflict = _conflictName(e);
      if (_nameConflict == null) {
        _quotaError = _quota(e);
        _error = _msg(e);
      }
      _isLoading = false;
      notifyListeners();
      return null;
    }
  }

  // ── Upload helpers ────────────────────────────────────────────────────────────

  /// Web-safe multipart upload using raw bytes — never touches dart:io.
  Future<Map<String, dynamic>> _uploadBytes({
    required List<int> bytes,
    required String filename,
    ImportConflictChoice? onConflict,
  }) async {
    final token = api.tokenForUpload;
    final url = Uri.parse('${api.baseUrl}/api/projects/import');
    final request = http.MultipartRequest(
      'POST',
      onConflict == null
          ? url
          : url.replace(queryParameters: {'on_conflict': onConflict.queryValue}),
    );
    if (token != null) {
      request.headers['Authorization'] = 'Bearer $token';
    }
    request.files.add(
      http.MultipartFile.fromBytes('file', bytes, filename: filename),
    );
    final streamed = await request.send();
    final res = await http.Response.fromStream(streamed);
    if (res.statusCode != 201) {
      throw ApiException(res.statusCode, res.body);
    }
    if (res.body.isEmpty) return {};
    return jsonDecode(res.body) as Map<String, dynamic>;
  }

  void setError(String msg) {
    _error = msg;
    _isLoading = false;
    notifyListeners();
  }

  // ── Error helper ──────────────────────────────────────────────────────────────

  String _msg(Exception e) {
    final s = e.toString();
    final m = RegExp(r'"detail"\s*:\s*"([^"]+)"').firstMatch(s);
    if (m != null) return m.group(1)!;
    // A 413 from a proxy in front of the server has no JSON detail (issue #434).
    if (e is ApiException && e.statusCode == 413) {
      return 'This file is too large to import.';
    }
    return s.replaceFirst('Exception: ', '');
  }

  /// The taken name of a 409 `name_conflict`, or null for any other failure.
  String? _conflictName(Exception e) {
    if (e is! ApiException || e.statusCode != 409) return null;
    try {
      final body = jsonDecode(e.body);
      if (body is Map && body['code'] == 'name_conflict' && body['name'] is String) {
        return body['name'] as String;
      }
    } on FormatException {
      // Not JSON: an ordinary failure.
    }
    return null;
  }

  /// A plan-limit refusal, or null for any other failure (issue #121).
  QuotaError? _quota(Exception e) =>
      e is ApiException ? QuotaError.fromApiException(e) : null;
}
