import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show Clipboard, ClipboardData;
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:vector_map_tiles/vector_map_tiles.dart' show VectorTileLayerMode;

import '../auth/auth_notifier.dart';
import '../auth/auth_service.dart';
import '../billing/billing_section.dart';
import '../core/app_version.dart';
import '../core/brand.dart';
import '../core/perf_timing.dart' show perfSpans;
import '../projects/basemaps.dart' show kMapTileModePref, mapTileModeNotifier;
import '../core/version_reload_stub.dart'
    if (dart.library.html) '../core/version_reload_web.dart';
import '../crypto/enable_encryption_screen.dart';
import '../crypto/encryption.dart';
import '../crypto/manage_devices_screen.dart';
import '../crypto/recover_screen.dart';
import '../projects/photo_thumb_cache.dart';
import '../projects/project_data_cache.dart';
import 'settings_service.dart';
import 'strava_oauth_popup_stub.dart'
    if (dart.library.js_interop) 'strava_oauth_popup_web.dart';
import 'theme_notifier.dart';

class SettingsScreen extends StatefulWidget {
  final SettingsService? service; // injectable for tests
  const SettingsScreen({super.key, this.service});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final SettingsService _service = widget.service ?? SettingsService();

  // ── Strava state ──────────────────────────────────────────────────────────
  bool _stravaConnected = false;
  bool _stravaLoading = false;
  final _stravaPopup = StravaOAuthPopup();

  // ── Polarsteps state ──────────────────────────────────────────────────────
  bool _polarstepsConnected = false;
  String? _polarstepsUsername;
  bool _polarstepsLoading = false;
  bool _polarstepsConnecting = false;
  bool _polarstepsUpdating = false; // reveal token field while already connected
  final _polarstepsTokenCtrl = TextEditingController();

  // ── Immich state ─────────────────────────────────────────────────────────
  bool _immichConnected = false;
  String? _immichServerUrl;
  bool _immichLoading = false;
  bool _immichSaving = false;
  final _immichServerUrlCtrl = TextEditingController();
  final _immichApiKeyCtrl = TextEditingController();

  // ── Backup state ──────────────────────────────────────────────────────────
  List<Map<String, dynamic>> _backups = [];
  bool _backupsLoading = false;
  String? _restoringDate;

  // ── Account state ─────────────────────────────────────────────────────────
  final _displayNameCtrl = TextEditingController();
  String _email = '';
  String _authProvider = 'local';
  bool _profileSaving = false;

  // Change-password state
  final _currentPwCtrl = TextEditingController();
  final _newPwCtrl = TextEditingController();
  final _confirmPwCtrl = TextEditingController();
  bool _pwChanging = false;
  bool _showCurrentPw = false;
  bool _showNewPw = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _loadStravaStatus();
      _loadPolarstepsStatus();
      _loadImmichStatus();
      _loadProfile();
      _loadBackups();
    });
  }

  @override
  void dispose() {
    _stravaPopup.dispose();
    _displayNameCtrl.dispose();
    _currentPwCtrl.dispose();
    _newPwCtrl.dispose();
    _confirmPwCtrl.dispose();
    _polarstepsTokenCtrl.dispose();
    _immichServerUrlCtrl.dispose();
    _immichApiKeyCtrl.dispose();
    super.dispose();
  }

  // ── Profile ───────────────────────────────────────────────────────────────

  Future<void> _loadProfile() async {
    try {
      final data = await _service.getProfile();
      if (mounted) {
        setState(() {
          _displayNameCtrl.text = data['display_name'] as String? ?? '';
          _email = data['email'] as String? ?? '';
          _authProvider = data['auth_provider'] as String? ?? 'local';
        });
      }
    } catch (_) {}
  }

  Future<void> _saveProfile() async {
    final name = _displayNameCtrl.text.trim();
    if (name.isEmpty) return;
    setState(() => _profileSaving = true);
    final auth = context.read<AuthNotifier>();
    try {
      final result = await _service.updateProfile(name);
      if (result.token != null) {
        await AuthService().persistToken(result.token!);
        auth.updateUser(result.user);
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Profile saved')),
        );
      }
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Save failed: ${e.toString().replaceFirst('Exception: ', '')}')),
        );
      }
    } finally {
      if (mounted) setState(() => _profileSaving = false);
    }
  }

  Future<void> _changePassword() async {
    final current = _currentPwCtrl.text;
    final next = _newPwCtrl.text;
    final confirm = _confirmPwCtrl.text;
    if (current.isEmpty || next.isEmpty) return;
    if (next != confirm) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('New passwords do not match')),
      );
      return;
    }
    setState(() => _pwChanging = true);
    try {
      await _service.changePassword(current: current, next: next);
      _currentPwCtrl.clear();
      _newPwCtrl.clear();
      _confirmPwCtrl.clear();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Password changed')),
        );
      }
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _pwChanging = false);
    }
  }

  Future<void> _deleteAccount() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete account?'),
        content: const Text(
          'This will permanently delete your account and all projects. '
          'This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          TextButton(
            style: TextButton.styleFrom(
              foregroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    final auth = context.read<AuthNotifier>();
    final router = GoRouter.of(context);
    try {
      await _service.deleteAccount();
      await auth.logout();
      router.go('/login');
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Delete failed: ${e.toString().replaceFirst('Exception: ', '')}')),
        );
      }
    }
  }

  Future<void> _clearCachedTripData() async {
    final messenger = ScaffoldMessenger.of(context);
    await projectDataCache.clearAll();
    await photoThumbCache.clearAll();
    if (!mounted) return;
    messenger.showSnackBar(
      const SnackBar(content: Text('Cached trip data cleared')),
    );
  }

  // ── Strava ────────────────────────────────────────────────────────────────

  Future<void> _loadStravaStatus() async {
    if (!mounted) return;
    setState(() => _stravaLoading = true);
    try {
      final connected = await _service.getStravaStatus();
      if (mounted) setState(() => _stravaConnected = connected);
    } catch (_) {
      // status not critical
    } finally {
      if (mounted) setState(() => _stravaLoading = false);
    }
  }

  Future<void> _connectStrava() async {
    try {
      final urlStr = await _service.getStravaConnectUrl();

      if (kIsWeb) {
        // Open OAuth in a popup and await the postMessage result relayed by
        // oauth_callback.html once the OAuth redirect completes.
        final result = await _stravaPopup.connect(urlStr);
        if (!mounted) return;
        if (result.connected) {
          _loadStravaStatus();
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('Strava connected!')),
          );
        } else {
          final reason = result.reason;
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(reason != null && reason.isNotEmpty
                  ? 'Strava connection failed: $reason'
                  : 'Strava connection failed.'),
            ),
          );
        }
      } else {
        await launchUrl(Uri.parse(urlStr),
            mode: LaunchMode.externalApplication);
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Could not open Strava: $e')),
        );
      }
    }
  }

  Future<void> _disconnectStrava() async {
    try {
      await _service.disconnectStrava();
      if (mounted) setState(() => _stravaConnected = false);
    } catch (_) {}
  }

  // ── Polarsteps ────────────────────────────────────────────────────────────

  Future<void> _loadPolarstepsStatus() async {
    if (!mounted) return;
    setState(() => _polarstepsLoading = true);
    try {
      final data = await _service.getPolarstepsStatus();
      if (mounted) {
        setState(() {
          _polarstepsConnected = data['connected'] == true;
          _polarstepsUsername = data['username'] as String?;
        });
      }
    } catch (_) {
      // status not critical
    } finally {
      if (mounted) setState(() => _polarstepsLoading = false);
    }
  }

  Future<void> _connectPolarsteps() async {
    final token = _polarstepsTokenCtrl.text.trim();
    if (token.isEmpty) return;
    setState(() => _polarstepsConnecting = true);
    try {
      final data = await _service.connectPolarsteps(token);
      if (mounted) {
        setState(() {
          _polarstepsConnected = true;
          _polarstepsUpdating = false;
          _polarstepsUsername = data['username'] as String?;
          _polarstepsTokenCtrl.clear();
        });
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Polarsteps connected!')),
        );
      }
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _polarstepsConnecting = false);
    }
  }

  Future<void> _disconnectPolarsteps() async {
    try {
      await _service.disconnectPolarsteps();
      if (mounted) {
        setState(() {
          _polarstepsConnected = false;
          _polarstepsUsername = null;
        });
      }
    } catch (_) {}
  }

  // ── Immich ────────────────────────────────────────────────────────────────

  Future<void> _loadImmichStatus() async {
    if (!mounted) return;
    setState(() => _immichLoading = true);
    try {
      final data = await _service.getImmichStatus();
      if (mounted) {
        setState(() {
          _immichConnected = data['connected'] == true;
          _immichServerUrl = data['server_url'] as String?;
        });
      }
    } catch (_) {
      // status not critical
    } finally {
      if (mounted) setState(() => _immichLoading = false);
    }
  }

  Future<void> _saveImmichConfig() async {
    final serverUrl = _immichServerUrlCtrl.text.trim();
    final apiKey = _immichApiKeyCtrl.text.trim();
    if (serverUrl.isEmpty || apiKey.isEmpty) return;
    setState(() => _immichSaving = true);
    try {
      await _service.saveImmichConfig(serverUrl: serverUrl, apiKey: apiKey);
      _immichServerUrlCtrl.clear();
      _immichApiKeyCtrl.clear();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Immich connected!')),
        );
      }
      await _loadImmichStatus();
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _immichSaving = false);
    }
  }

  Future<void> _disconnectImmich() async {
    try {
      await _service.disconnectImmich();
      if (mounted) {
        setState(() {
          _immichConnected = false;
          _immichServerUrl = null;
        });
      }
    } catch (_) {}
  }

  // ── Backups ───────────────────────────────────────────────────────────────

  Future<void> _loadBackups() async {
    if (!mounted) return;
    setState(() => _backupsLoading = true);
    try {
      final data = await _service.listBackups();
      if (mounted) setState(() => _backups = data);
    } catch (_) {
    } finally {
      if (mounted) setState(() => _backupsLoading = false);
    }
  }

  Future<void> _restoreBackup(String date) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Restore backup?'),
        content: Text(
          'This will replace the entire database with the backup from $date. '
          'The app will reload after the restore.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          TextButton(
            style: TextButton.styleFrom(
              foregroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Restore'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _restoringDate = date);
    try {
      await _service.restoreBackup(date);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Restore complete — reloading…')),
        );
        // Force a full page reload so Flutter re-fetches everything from the restored DB.
        reloadApp();
      }
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Restore failed: ${e.toString().replaceFirst('Exception: ', '')}')),
        );
      }
    } finally {
      if (mounted) setState(() => _restoringDate = null);
    }
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final themeNotifier = context.watch<ThemeNotifier>();

    return Scaffold(
      appBar: AppBar(
        // A checkout redirect lands here as a fresh navigation (issue #192),
        // so there is nothing to pop back to — fall back to the app's home
        // rather than let the arrow silently do nothing.
        leading: BackButton(
          onPressed: () => context.canPop() ? context.pop() : context.go('/'),
        ),
        title: const Text('Settings'),
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                // ── Appearance ─────────────────────────────────────────
                _SectionCard(
                  title: 'Appearance',
                  icon: Icons.palette_outlined,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Theme', style: theme.textTheme.bodySmall),
                      const SizedBox(height: 10),
                      SegmentedButton<ThemeMode>(
                        segments: const [
                          ButtonSegment(
                            value: ThemeMode.light,
                            icon: Icon(Icons.light_mode_outlined),
                            label: Text('Light'),
                          ),
                          ButtonSegment(
                            value: ThemeMode.system,
                            icon: Icon(Icons.brightness_auto_outlined),
                            label: Text('System'),
                          ),
                          ButtonSegment(
                            value: ThemeMode.dark,
                            icon: Icon(Icons.dark_mode_outlined),
                            label: Text('Dark'),
                          ),
                        ],
                        selected: {themeNotifier.mode},
                        onSelectionChanged: (s) =>
                            themeNotifier.setMode(s.first),
                        multiSelectionEnabled: false,
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 16),

                // ── Plan ───────────────────────────────────────────────
                // Renders nothing on a self-hosted instance (issue #121).
                const BillingSection(),

                // ── Account ────────────────────────────────────────────
                _SectionCard(
                  title: 'Account',
                  icon: Icons.person_outlined,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      TextField(
                        controller: _displayNameCtrl,
                        decoration: const InputDecoration(
                          labelText: 'Display name',
                          border: OutlineInputBorder(),
                        ),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        readOnly: true,
                        decoration: InputDecoration(
                          labelText: 'Email',
                          border: const OutlineInputBorder(),
                          filled: true,
                          fillColor: theme.colorScheme.surfaceContainerHighest
                              .withValues(alpha: 0.4),
                        ),
                        controller:
                            TextEditingController(text: _email),
                        style: TextStyle(
                            color: theme.colorScheme.onSurface
                                .withValues(alpha: 0.5)),
                      ),
                      const SizedBox(height: 16),
                      Align(
                        alignment: Alignment.centerRight,
                        child: FilledButton(
                          onPressed: _profileSaving ? null : _saveProfile,
                          child: _profileSaving
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(
                                      strokeWidth: 2))
                              : const Text('Save'),
                        ),
                      ),

                      // Change-password subsection (local accounts only)
                      if (_authProvider == 'local') ...[
                        const Divider(height: 32),
                        Text('Change password',
                            style: theme.textTheme.titleSmall),
                        const SizedBox(height: 12),
                        TextField(
                          controller: _currentPwCtrl,
                          obscureText: !_showCurrentPw,
                          decoration: InputDecoration(
                            labelText: 'Current password',
                            border: const OutlineInputBorder(),
                            suffixIcon: IconButton(
                              icon: Icon(_showCurrentPw
                                  ? Icons.visibility_off_outlined
                                  : Icons.visibility_outlined),
                              onPressed: () => setState(
                                  () => _showCurrentPw = !_showCurrentPw),
                            ),
                          ),
                        ),
                        const SizedBox(height: 12),
                        TextField(
                          controller: _newPwCtrl,
                          obscureText: !_showNewPw,
                          decoration: InputDecoration(
                            labelText: 'New password',
                            border: const OutlineInputBorder(),
                            suffixIcon: IconButton(
                              icon: Icon(_showNewPw
                                  ? Icons.visibility_off_outlined
                                  : Icons.visibility_outlined),
                              onPressed: () =>
                                  setState(() => _showNewPw = !_showNewPw),
                            ),
                          ),
                        ),
                        const SizedBox(height: 12),
                        TextField(
                          controller: _confirmPwCtrl,
                          obscureText: true,
                          decoration: const InputDecoration(
                            labelText: 'Confirm new password',
                            border: OutlineInputBorder(),
                          ),
                        ),
                        const SizedBox(height: 16),
                        Align(
                          alignment: Alignment.centerRight,
                          child: OutlinedButton(
                            onPressed: _pwChanging ? null : _changePassword,
                            child: _pwChanging
                                ? const SizedBox(
                                    width: 18,
                                    height: 18,
                                    child: CircularProgressIndicator(
                                        strokeWidth: 2))
                                : const Text('Change password'),
                          ),
                        ),
                      ],
                    ],
                  ),
                ),

                const SizedBox(height: 16),

                // ── Strava ─────────────────────────────────────────────
                _SectionCard(
                  title: 'Strava',
                  icon: Icons.directions_run,
                  child: _stravaLoading
                      ? const Padding(
                          padding: EdgeInsets.symmetric(vertical: 8),
                          child: Center(child: CircularProgressIndicator()),
                        )
                      : Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            if (_stravaConnected) ...[
                              Row(
                                children: [
                                  Icon(Icons.check_circle,
                                      color: theme.colorScheme.primary,
                                      size: 18),
                                  const SizedBox(width: 8),
                                  Text('Connected',
                                      style: theme.textTheme.bodyMedium),
                                  const Spacer(),
                                  TextButton(
                                    onPressed: _disconnectStrava,
                                    child: const Text('Disconnect'),
                                  ),
                                ],
                              ),
                              const SizedBox(height: 4),
                              Text(
                                'Open a project and use the Import button in the toolbar to add Strava activities.',
                                style: theme.textTheme.bodySmall,
                              ),
                            ] else ...[
                              Text(
                                'Connect your Strava account to import activities into projects.',
                                style: theme.textTheme.bodySmall,
                              ),
                              const SizedBox(height: 16),
                              OutlinedButton.icon(
                                onPressed: _connectStrava,
                                icon: const Icon(Icons.link),
                                label: const Text('Connect Strava'),
                              ),
                            ],
                          ],
                        ),
                ),

                const SizedBox(height: 16),

                // ── Polarsteps ─────────────────────────────────────────
                _SectionCard(
                  title: 'Polarsteps',
                  icon: Icons.explore_outlined,
                  child: _polarstepsLoading
                      ? const Padding(
                          padding: EdgeInsets.symmetric(vertical: 8),
                          child: Center(child: CircularProgressIndicator()),
                        )
                      : Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            if (_polarstepsConnected) ...[
                              Row(
                                children: [
                                  Icon(Icons.check_circle,
                                      color: theme.colorScheme.primary,
                                      size: 18),
                                  const SizedBox(width: 8),
                                  Expanded(
                                    child: Text(
                                      _polarstepsUsername != null
                                          ? 'Connected as @$_polarstepsUsername'
                                          : 'Connected',
                                      style: theme.textTheme.bodyMedium,
                                    ),
                                  ),
                                  TextButton(
                                    onPressed: () => setState(() =>
                                        _polarstepsUpdating = !_polarstepsUpdating),
                                    child: Text(_polarstepsUpdating
                                        ? 'Cancel'
                                        : 'Update token'),
                                  ),
                                  TextButton(
                                    onPressed: _disconnectPolarsteps,
                                    child: const Text('Disconnect'),
                                  ),
                                ],
                              ),
                              const SizedBox(height: 4),
                              if (_polarstepsUpdating) ...[
                                Text(
                                  'Paste a fresh remember_token if your session expired '
                                  '(no need to disconnect first).',
                                  style: theme.textTheme.bodySmall?.copyWith(
                                    color: theme.colorScheme.onSurfaceVariant,
                                  ),
                                ),
                                const SizedBox(height: 12),
                                TextField(
                                  controller: _polarstepsTokenCtrl,
                                  decoration: const InputDecoration(
                                    labelText: 'remember_token',
                                    hintText: 'Paste cookie value here…',
                                    border: OutlineInputBorder(),
                                    isDense: true,
                                  ),
                                  obscureText: true,
                                ),
                                const SizedBox(height: 12),
                                FilledButton(
                                  onPressed: _polarstepsConnecting
                                      ? null
                                      : _connectPolarsteps,
                                  child: _polarstepsConnecting
                                      ? const SizedBox(
                                          width: 16,
                                          height: 16,
                                          child: CircularProgressIndicator(
                                              strokeWidth: 2,
                                              color: Colors.white),
                                        )
                                      : const Text('Update token'),
                                ),
                              ] else
                                Text(
                                  'Open a project and use the Import button in the toolbar to import Polarsteps memories.',
                                  style: theme.textTheme.bodySmall,
                                ),
                            ] else ...[
                              Text(
                                'Connect your Polarsteps account to import steps as memories.',
                                style: theme.textTheme.bodySmall,
                              ),
                              const SizedBox(height: 4),
                              Text(
                                'In your browser: DevTools → Application → Cookies → polarsteps.com → copy the remember_token value.',
                                style: theme.textTheme.bodySmall?.copyWith(
                                  color: theme.colorScheme.onSurfaceVariant,
                                ),
                              ),
                              const SizedBox(height: 12),
                              TextField(
                                controller: _polarstepsTokenCtrl,
                                decoration: const InputDecoration(
                                  labelText: 'remember_token',
                                  hintText: 'Paste cookie value here…',
                                  border: OutlineInputBorder(),
                                  isDense: true,
                                ),
                                obscureText: true,
                              ),
                              const SizedBox(height: 12),
                              FilledButton(
                                onPressed: _polarstepsConnecting
                                    ? null
                                    : _connectPolarsteps,
                                child: _polarstepsConnecting
                                    ? const SizedBox(
                                        width: 16,
                                        height: 16,
                                        child: CircularProgressIndicator(
                                            strokeWidth: 2,
                                            color: Colors.white),
                                      )
                                    : const Text('Connect Polarsteps'),
                              ),
                            ],
                          ],
                        ),
                ),

                const SizedBox(height: 16),

                // ── Immich ─────────────────────────────────────────────
                _SectionCard(
                  title: 'Immich',
                  icon: Icons.photo_library_outlined,
                  child: _immichLoading
                      ? const Padding(
                          padding: EdgeInsets.symmetric(vertical: 8),
                          child: Center(child: CircularProgressIndicator()),
                        )
                      : Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            if (_immichConnected) ...[
                              Row(
                                children: [
                                  Icon(Icons.check_circle,
                                      color: theme.colorScheme.primary,
                                      size: 18),
                                  const SizedBox(width: 8),
                                  Expanded(
                                    child: Text(
                                      _immichServerUrl != null
                                          ? 'Connected to $_immichServerUrl'
                                          : 'Connected',
                                      style: theme.textTheme.bodyMedium,
                                    ),
                                  ),
                                  TextButton(
                                    onPressed: _disconnectImmich,
                                    child: const Text('Disconnect'),
                                  ),
                                ],
                              ),
                            ] else ...[
                              Text(
                                'Connect your self-hosted Immich server to pull higher-quality photos when upgrading trip photos.',
                                style: theme.textTheme.bodySmall,
                              ),
                              const SizedBox(height: 12),
                              TextField(
                                controller: _immichServerUrlCtrl,
                                decoration: const InputDecoration(
                                  labelText: 'Server URL',
                                  hintText: 'https://photos.example.com',
                                  border: OutlineInputBorder(),
                                  isDense: true,
                                ),
                              ),
                              const SizedBox(height: 12),
                              TextField(
                                controller: _immichApiKeyCtrl,
                                decoration: const InputDecoration(
                                  labelText: 'API key',
                                  border: OutlineInputBorder(),
                                  isDense: true,
                                ),
                                obscureText: true,
                              ),
                              const SizedBox(height: 12),
                              FilledButton(
                                onPressed: _immichSaving ? null : _saveImmichConfig,
                                child: _immichSaving
                                    ? const SizedBox(
                                        width: 16,
                                        height: 16,
                                        child: CircularProgressIndicator(
                                            strokeWidth: 2,
                                            color: Colors.white),
                                      )
                                    : const Text('Save'),
                              ),
                            ],
                          ],
                        ),
                ),

                const SizedBox(height: 16),

                // ── Backups ────────────────────────────────────────────
                _SectionCard(
                  title: 'Backups',
                  icon: Icons.history_outlined,
                  child: _backupsLoading
                      ? const Padding(
                          padding: EdgeInsets.symmetric(vertical: 8),
                          child: Center(child: CircularProgressIndicator()),
                        )
                      : _backups.isEmpty
                          ? Text(
                              'No backups yet. The first backup will be created automatically at 02:00 UTC.',
                              style: theme.textTheme.bodySmall,
                            )
                          : Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                Text(
                                  'Daily backups are kept for 30 days. Restoring replaces the entire database.',
                                  style: theme.textTheme.bodySmall,
                                ),
                                const SizedBox(height: 12),
                                ..._backups.map((b) {
                                  final date = b['date'] as String;
                                  final bytes = b['size_bytes'] as int;
                                  final sizeLabel = bytes < 1024 * 1024
                                      ? '${(bytes / 1024).toStringAsFixed(0)} KB'
                                      : '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
                                  final isRestoring = _restoringDate == date;
                                  return Padding(
                                    padding: const EdgeInsets.only(bottom: 6),
                                    child: Row(
                                      children: [
                                        Expanded(
                                          child: Column(
                                            crossAxisAlignment: CrossAxisAlignment.start,
                                            children: [
                                              Text(date, style: theme.textTheme.bodyMedium),
                                              Text(sizeLabel, style: theme.textTheme.bodySmall),
                                            ],
                                          ),
                                        ),
                                        isRestoring
                                            ? const SizedBox(
                                                width: 20,
                                                height: 20,
                                                child: CircularProgressIndicator(strokeWidth: 2),
                                              )
                                            : TextButton(
                                                onPressed: _restoringDate != null
                                                    ? null
                                                    : () => _restoreBackup(date),
                                                child: const Text('Restore'),
                                              ),
                                      ],
                                    ),
                                  );
                                }),
                              ],
                            ),
                ),

                const SizedBox(height: 16),

                // ── Encryption ─────────────────────────────────────────
                _SectionCard(
                  title: 'Encryption',
                  icon: Icons.lock_outline,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Lock your memories and journal with a key only you hold — '
                        'so not even an administrator can read them.',
                        style: theme.textTheme.bodySmall,
                      ),
                      const SizedBox(height: 16),
                      FilledButton.icon(
                        onPressed: () {
                          Navigator.of(context).push(MaterialPageRoute(
                            builder: (_) =>
                                EnableEncryptionScreen(service: encryption),
                          ));
                        },
                        icon: const Icon(Icons.lock_outline),
                        label: const Text('Set up encryption'),
                      ),
                      const SizedBox(height: 8),
                      TextButton.icon(
                        onPressed: () {
                          Navigator.of(context).push(MaterialPageRoute(
                            builder: (_) =>
                                ManageDevicesScreen(service: encryption),
                          ));
                        },
                        icon: const Icon(Icons.devices_other),
                        label: const Text('Approve another device'),
                      ),
                      TextButton.icon(
                        onPressed: () {
                          Navigator.of(context).push(MaterialPageRoute(
                            builder: (_) => RecoverScreen(service: encryption),
                          ));
                        },
                        icon: const Icon(Icons.lock_open),
                        label: const Text('Recover access'),
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 16),

                // ── Admin (admins only) ────────────────────────────────
                if (context.watch<AuthNotifier>().user?.isAdmin ?? false) ...[
                  _SectionCard(
                    title: 'Admin',
                    icon: Icons.admin_panel_settings_outlined,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Deployment usage — users, storage, project counts, '
                          'and user password reset.',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                        const SizedBox(height: 16),
                        FilledButton.icon(
                          onPressed: () => context.push('/admin'),
                          icon: const Icon(Icons.dashboard_outlined),
                          label: const Text('Open admin dashboard'),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 16),
                ],

                // ── Storage (on-device cache — native only, no local
                // filesystem to cache to on web) ────────────────────────
                if (!kIsWeb) ...[
                  _SectionCard(
                    title: 'Storage',
                    icon: Icons.storage_outlined,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Trips you\'ve opened are cached on this device so '
                          'reopening them doesn\'t re-download the map and '
                          'elevation data every time.',
                          style: theme.textTheme.bodySmall,
                        ),
                        const SizedBox(height: 16),
                        OutlinedButton(
                          onPressed: _clearCachedTripData,
                          child: const Text('Clear cached trip data'),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 16),
                ],

                // ── Danger Zone ────────────────────────────────────────
                _SectionCard(
                  title: 'Danger Zone',
                  icon: Icons.warning_amber_outlined,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Permanently delete your account and all associated projects and data.',
                        style: theme.textTheme.bodySmall,
                      ),
                      const SizedBox(height: 16),
                      OutlinedButton(
                        style: OutlinedButton.styleFrom(
                          foregroundColor: theme.colorScheme.error,
                          side: BorderSide(color: theme.colorScheme.error),
                        ),
                        onPressed: _deleteAccount,
                        child: const Text('Delete my account'),
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 16),

                // ── Map rendering ──────────────────────────────────────
                const _TileModePanel(),

                const SizedBox(height: 16),

                // ── Performance ────────────────────────────────────────
                // Surfaced in the app, not behind a --dart-define debugPrint,
                // because the builds that need diagnosing are the ones on a
                // phone with no debugger attached (issue #291).
                const _PerfPanel(),

                const SizedBox(height: 16),

                // ── About ──────────────────────────────────────────────
                _SectionCard(
                  title: 'About',
                  icon: Icons.info_outlined,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(kAppName,
                          style: theme.textTheme.titleMedium),
                      const SizedBox(height: 4),
                      // Both halves, named: this is the line a user is asked
                      // to read out in a bug report (#275).
                      const VersionText(
                        style: TextStyle(
                          fontFamily: 'monospace',
                          fontSize: 12,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        '© ${DateTime.now().year} Rui Narciso. All rights reserved.',
                        style: theme.textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 32),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// ── Reusable section card ─────────────────────────────────────────────────────

class _SectionCard extends StatelessWidget {
  final String title;
  final IconData icon;
  final Widget child;

  const _SectionCard({
    required this.title,
    required this.icon,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: theme.colorScheme.primary, size: 20),
                const SizedBox(width: 8),
                Text(title, style: theme.textTheme.titleLarge),
              ],
            ),
            const Divider(height: 24),
            child,
          ],
        ),
      ),
    );
  }
}

/// Live performance report (issues #291, #276).
///
/// Rendered from [perfSpans]' current state rather than from the snapshot
/// taken when a load finished, so a user can pan the map, come here, and see
/// the frames that pan actually cost. The load report alone was misleading:
/// it stayed comfortably under budget while panning was still tripping the
/// ANR watchdog, because nothing measured the gesture.
class _PerfPanel extends StatefulWidget {
  const _PerfPanel();

  @override
  State<_PerfPanel> createState() => _PerfPanelState();
}

class _PerfPanelState extends State<_PerfPanel> {
  // Deliberately not assembling perfFullReport's argument list here: this
  // panel is the report a human reads, and hand-building it is how it came to
  // be missing four instruments (see PerfSpans.buildReport).
  String _report() => perfSpans.buildReport();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final report = _report();
    final empty = perfSpans.blockingSpans.isEmpty &&
        perfSpans.stageSpans.isEmpty &&
        perfSpans.gestureFrames.gestures == 0 &&
        perfSpans.diagnostics.stallTicks == 0;

    return _SectionCard(
      title: 'Performance',
      icon: Icons.speed_outlined,
      child: empty
          ? Text(
              'Open a trip, pan the map, then come back here to see where the '
              'time went.',
              style: theme.textTheme.bodySmall,
            )
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Timings from the last trip you opened, and from any map '
                  'panning since.',
                  style: theme.textTheme.bodySmall,
                ),
                const SizedBox(height: 8),
                // Fixed-width content: scroll rather than wrap, which would
                // scramble the columns.
                SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: SelectableText(
                    report,
                    style: const TextStyle(
                        fontFamily: 'monospace', fontSize: 11),
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    TextButton.icon(
                      icon: const Icon(Icons.refresh, size: 16),
                      label: const Text('Refresh'),
                      onPressed: () => setState(() {}),
                    ),
                    TextButton.icon(
                      icon: const Icon(Icons.copy, size: 16),
                      label: const Text('Copy'),
                      onPressed: () async {
                        await Clipboard.setData(ClipboardData(text: report));
                        if (!context.mounted) return;
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text('Timings copied')),
                        );
                      },
                    ),
                  ],
                ),
              ],
            ),
    );
  }
}


/// Runtime basemap render-mode switch (issue #276).
///
/// `vector` repaints the whole basemap on every pumped frame; `raster`
/// renders each tile to a cached bitmap once. The project has been parked on
/// `vector` behind a `--dart-define` since that trade-off was measured on
/// CanvasKit, which meant comparing them on a phone required a custom build.
/// Exposing it here makes it a one-tap experiment on the build you already
/// have.
class _TileModePanel extends StatefulWidget {
  const _TileModePanel();

  @override
  State<_TileModePanel> createState() => _TileModePanelState();
}

class _TileModePanelState extends State<_TileModePanel> {
  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((prefs) {
      final saved = prefs.getString(kMapTileModePref);
      if (saved == null || !mounted) return;
      mapTileModeNotifier.value = saved == 'raster'
          ? VectorTileLayerMode.raster
          : VectorTileLayerMode.vector;
    });
  }

  Future<void> _set(VectorTileLayerMode mode) async {
    mapTileModeNotifier.value = mode;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(
        kMapTileModePref,
        mode == VectorTileLayerMode.raster ? 'raster' : 'vector');
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return _SectionCard(
      title: 'Map rendering',
      icon: Icons.layers_outlined,
      child: ValueListenableBuilder<VectorTileLayerMode>(
        valueListenable: mapTileModeNotifier,
        builder: (context, mode, __) => Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Cache map tiles as images'),
              subtitle: Text(
                'Smoother panning on long trips; tiles look briefly soft '
                'while zooming. Reopen the map to apply.',
                style: theme.textTheme.bodySmall,
              ),
              value: mode == VectorTileLayerMode.raster,
              onChanged: (on) => _set(
                  on ? VectorTileLayerMode.raster : VectorTileLayerMode.vector),
            ),
          ],
        ),
      ),
    );
  }
}
