// ignore_for_file: deprecated_member_use
import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/material.dart';

import '../api/client.dart';
import '../crypto/share_crypto.dart';
import '../photos/photo_upgrade_screen.dart';
import '../shared/shared_memory_service.dart';
import 'memory_dialog.dart';
import 'project_memory_crud_mixin.dart' show TranslationUnavailableException;
import 'project_notifier.dart';
import 'social_share_dialog.dart';
import '../crypto/encrypted_display.dart';

/// Shows a detail view for a memory — photo mosaic, date, description,
/// likes, comments, prev/next navigation and edit/delete controls.
///
/// Desktop (≥640px): two-column split — photo mosaic left, content right.
/// Mobile (<640px): hero photo on top, scrollable content below.
///
/// Pass [shareToken] when opening from a shared-project view so that
/// comment/like API calls go to the share endpoints. Pass [shareContentKey]
/// (issue #28) when the viewer has one, so an encrypted memory's
/// `share_name_ciphertext`/`share_description_ciphertext` can be decrypted.
void showMemoryDetail(
  BuildContext context,
  ProjectNotifier notifier,
  Map<String, dynamic> memory, {
  bool readOnly = false,
  String? shareToken,
  SecretKey? shareContentKey,
}) {
  showDialog(
    context: context,
    useRootNavigator: true,
    builder: (_) => _MemoryDetailModal(
      notifier: notifier,
      memory: memory,
      readOnly: readOnly,
      shareToken: shareToken,
      shareContentKey: shareContentKey,
    ),
  );
}

// ── Design tokens ─────────────────────────────────────────────────────────────
const _kBg     = Color(0xFF0F1A26);
const _kBgDark = Color(0xFF0C1722);
const _kBorder = Color(0xFF1F2F42);
const _kText1  = Color(0xFFE2E8F0);
const _kText2  = Color(0xFFCBD5E1);
const _kDim    = Color(0xFF64748B);
const _kOrange = Color(0xFFFC4C02);
const _kBlue   = Color(0xFF1D4ED8);
const _kRed    = Color(0xFFEF4444);

class _MemoryDetailModal extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> memory;
  final bool readOnly;
  final String? shareToken;
  final SecretKey? shareContentKey;

  const _MemoryDetailModal({
    required this.notifier,
    required this.memory,
    this.readOnly = false,
    this.shareToken,
    this.shareContentKey,
  });

  @override
  State<_MemoryDetailModal> createState() => _MemoryDetailModalState();
}

class _MemoryDetailModalState extends State<_MemoryDetailModal> {
  late Map<String, dynamic> _current;
  int _photoViewerIndex = -1;

  // ── Likes state ───────────────────────────────────────────────────────────
  int _likeCount = 0;
  bool _likedByMe = false;
  List<Map<String, dynamic>> _likers = [];
  bool _likeBusy = false;

  // ── Comments state ────────────────────────────────────────────────────────
  List<Map<String, dynamic>> _comments = [];
  bool _commentsLoading = false;
  int? _replyToId;
  String? _replyToName;
  final _commentCtrl = TextEditingController();
  bool _submitting = false;

  // ── Translation state ─────────────────────────────────────────────────────
  String? _activeLang;
  final Map<String, Map<String, dynamic>> _translationCache = {};
  bool _translating = false;

  // ── Share-encrypted content state (issue #28) ────────────────────────────
  // Set when the memory carries share_name_ciphertext/share_description_ciphertext
  // and widget.shareContentKey successfully decrypts them. When they stay
  // null despite the memory being marked encrypted (see _nameUnavailable /
  // _descriptionUnavailable below), decryption either wasn't attempted (no
  // key) or failed (wrong key / tamper).
  String? _decryptedShareName;
  String? _decryptedShareDescription;

  static const _kLangFlags = {
    'en': '🇬🇧', 'pt': '🇵🇹', 'fr': '🇫🇷', 'de': '🇩🇪',
    'es': '🇪🇸', 'it': '🇮🇹', 'nl': '🇳🇱', 'ja': '🇯🇵',
    'zh': '🇨🇳', 'ru': '🇷🇺', 'ar': '🇸🇦', 'ko': '🇰🇷',
  };

  static const _months = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];

  static String _fmtDate(String? ds) {
    if (ds == null) return '';
    final d = DateTime.tryParse(ds);
    if (d == null) return ds;
    return '${_months[d.month - 1]} ${d.day}, ${d.year}';
  }

  String get _memoryId => _current['id']?.toString() ?? '';

  @override
  void initState() {
    super.initState();
    _current = widget.memory;
    _likeCount = (_current['like_count'] as num?)?.toInt() ?? 0;
    _loadLikes();
    _loadComments();
    _maybeDecryptShareContent();
  }

  /// Decrypt share_name_ciphertext/share_description_ciphertext (issue #28)
  /// with widget.shareContentKey, if the memory carries either. No-op when
  /// the memory has no share ciphertext at all (the common case — most
  /// memories are either plaintext or the owner never generated share
  /// content). Leaves [_decryptedShareName]/[_decryptedShareDescription] null
  /// — and so [_nameUnavailable]/[_descriptionUnavailable] true — when there's
  /// no key, or the key doesn't decrypt it.
  Future<void> _maybeDecryptShareContent() async {
    final nameCt = _current['share_name_ciphertext'] as String?;
    final descCt = _current['share_description_ciphertext'] as String?;
    if (nameCt == null && descCt == null) return;

    final key = widget.shareContentKey;
    if (key == null) return; // runs before first build — no setState needed

    try {
      final name = nameCt != null ? await decryptTextWithKey(nameCt, key) : null;
      final desc = descCt != null ? await decryptTextWithKey(descCt, key) : null;
      if (!mounted) return;
      setState(() {
        _decryptedShareName = name;
        _decryptedShareDescription = desc;
      });
    } catch (_) {
      // Wrong key / tampered ciphertext — _nameUnavailable / _descriptionUnavailable
      // already read true from the unset decrypted fields; nothing to rebuild.
    }
  }

  @override
  void dispose() {
    _commentCtrl.dispose();
    super.dispose();
  }

  // ── Data loaders ──────────────────────────────────────────────────────────

  Future<void> _loadLikes() async {
    try {
      final Map<String, dynamic> data;
      final id = int.tryParse(_memoryId);
      if (widget.shareToken != null && id != null) {
        data = await sharedMemoryService.fetchLikes(widget.shareToken!, id);
      } else {
        data = await widget.notifier.fetchLikes(_memoryId);
      }
      if (mounted) {
        setState(() {
          _likeCount   = (data['count'] as num?)?.toInt() ?? 0;
          _likedByMe   = data['liked_by_me'] as bool? ?? false;
          _likers      = (data['likers'] as List?)
                ?.cast<Map<String, dynamic>>() ?? [];
        });
      }
    } catch (_) {}
  }

  Future<void> _loadComments() async {
    if (!mounted) return;
    setState(() => _commentsLoading = true);
    try {
      final List<Map<String, dynamic>> data;
      final id = int.tryParse(_memoryId);
      if (widget.shareToken != null && id != null) {
        data = await sharedMemoryService.fetchComments(widget.shareToken!, id);
      } else {
        data = await widget.notifier.fetchComments(_memoryId);
      }
      if (mounted) setState(() => _comments = data);
    } catch (_) {
    } finally {
      if (mounted) setState(() => _commentsLoading = false);
    }
  }

  // ── Like / unlike ─────────────────────────────────────────────────────────

  Future<void> _toggleLike() async {
    if (_likeBusy || !api.isAuthenticated) return;
    setState(() {
      _likeBusy  = true;
      _likedByMe = !_likedByMe;
      _likeCount += _likedByMe ? 1 : -1;
    });
    try {
      final id = int.tryParse(_memoryId);
      if (widget.shareToken != null && id != null) {
        if (_likedByMe) {
          await sharedMemoryService.likeMemory(widget.shareToken!, id);
        } else {
          await sharedMemoryService.unlikeMemory(widget.shareToken!, id);
        }
      } else {
        if (_likedByMe) {
          await widget.notifier.likeMemory(_memoryId);
        } else {
          await widget.notifier.unlikeMemory(_memoryId);
        }
      }
      await _loadLikes();
    } catch (_) {
      // Revert optimistic update
      if (mounted) {
        setState(() {
          _likedByMe = !_likedByMe;
          _likeCount += _likedByMe ? 1 : -1;
        });
      }
    } finally {
      if (mounted) setState(() => _likeBusy = false);
    }
  }

  // ── Comment submit ────────────────────────────────────────────────────────

  Future<void> _submitComment() async {
    final text = _commentCtrl.text.trim();
    if (text.isEmpty || _submitting) return;
    setState(() => _submitting = true);
    try {
      final id = int.tryParse(_memoryId);
      if (widget.shareToken != null && id != null) {
        await sharedMemoryService.addComment(
          widget.shareToken!, id, text,
          parentCommentId: _replyToId,
        );
      } else {
        await widget.notifier.addComment(
          _memoryId, text,
          parentCommentId: _replyToId,
        );
      }
      _commentCtrl.clear();
      if (mounted) setState(() { _replyToId = null; _replyToName = null; });
      await _loadComments();
    } catch (_) {
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _deleteComment(int commentId) async {
    try {
      if (widget.shareToken != null) {
        final id = int.tryParse(_memoryId);
        if (id != null) {
          await sharedMemoryService.deleteComment(
              widget.shareToken!, id, commentId);
        }
      } else {
        await widget.notifier.deleteComment(_memoryId, commentId);
      }
      await _loadComments();
    } catch (_) {}
  }

  // ── Translation ───────────────────────────────────────────────────────────

  String? get _displayName {
    if (_activeLang != null && _translationCache.containsKey(_activeLang!)) {
      return _translationCache[_activeLang!]!['name'] as String?;
    }
    if (_decryptedShareName != null) return _decryptedShareName;
    return shownText(_current['name'] as String?);
  }

  String get _displayDescription {
    if (_activeLang != null && _translationCache.containsKey(_activeLang!)) {
      return (_translationCache[_activeLang!]!['description'] as String?) ?? '';
    }
    if (_decryptedShareDescription != null) return _decryptedShareDescription!;
    return shownText(_current['description'] as String?) ?? '';
  }

  /// True when the server marked the name as E2EE (issue #26) but this
  /// viewer couldn't recover it — either the owner never generated share
  /// content (issue #28) for this memory, or this viewer has no/the wrong
  /// fragment key. Distinct from a memory that simply has no title.
  bool get _nameUnavailable =>
      _current['name_encrypted'] == true && _decryptedShareName == null;

  bool get _descriptionUnavailable =>
      _current['description_encrypted'] == true &&
      _decryptedShareDescription == null;

  Future<void> _loadTranslation(String langCode) async {
    if (_translationCache.containsKey(langCode)) {
      setState(() => _activeLang = langCode);
      return;
    }
    setState(() => _translating = true);
    try {
      final Map<String, dynamic> data;
      final id = int.tryParse(_memoryId);
      if (widget.shareToken != null && id != null) {
        data = await sharedMemoryService.fetchTranslation(
            widget.shareToken!, id, langCode);
      } else {
        data = await widget.notifier.fetchTranslation(_memoryId, langCode);
      }
      if (mounted) {
        setState(() {
          _translationCache[langCode] = data;
          _activeLang = langCode;
        });
      }
    } catch (e) {
      debugPrint('Translation failed for "$langCode": $e');
      if (mounted) {
        // Encrypted memories can never be translated — retrying is pointless,
        // so this gets its own message instead of the generic transient-error one.
        final message = e is TranslationUnavailableException
            ? 'This memory is encrypted and can\'t be translated.'
            : 'Couldn\'t translate${e is ApiException ? ' (${e.statusCode})' : ''}. Please try again.';
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(message)),
        );
      }
    } finally {
      if (mounted) setState(() => _translating = false);
    }
  }

  Widget _languageBar(List<String> langs) {
    if (langs.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 6),
      child: Row(
        children: [
          // "original" reset button
          _LangButton(
            label: 'Original',
            active: _activeLang == null,
            loading: false,
            onTap: () => setState(() => _activeLang = null),
          ),
          const SizedBox(width: 4),
          ...langs.map((code) => Padding(
            padding: const EdgeInsets.only(right: 4),
            child: _LangButton(
              label: _kLangFlags[code] ?? code.toUpperCase(),
              active: _activeLang == code,
              loading: _translating && _activeLang != code,
              onTap: () => _loadTranslation(code),
            ),
          )),
        ],
      ),
    );
  }

  // ── Navigation ────────────────────────────────────────────────────────────

  List<Map<String, dynamic>> get _allMemories => widget.notifier.items
      .where((i) => i['item_type'] == 'memory')
      .map((i) => i['memory'] as Map<String, dynamic>?)
      .whereType<Map<String, dynamic>>()
      .toList();

  int get _currentIndex {
    final id = _current['id']?.toString();
    return _allMemories.indexWhere((m) => m['id']?.toString() == id);
  }

  void _navigate(int delta) {
    final mems = _allMemories;
    if (mems.isEmpty) return;
    final next = (_currentIndex + delta).clamp(0, mems.length - 1);
    setState(() {
      _current           = mems[next];
      _comments          = [];
      _likeCount         = (_current['like_count'] as num?)?.toInt() ?? 0;
      _likedByMe         = false;
      _likers            = [];
      _replyToId         = null;
      _replyToName       = null;
      _activeLang        = null;
      _translating       = false;
      _commentCtrl.clear();
    });
    _loadLikes();
    _loadComments();
  }

  Future<void> _edit() async {
    Navigator.of(context, rootNavigator: true).pop();
    await showDialog(
      context: context,
      useRootNavigator: true,
      builder: (_) => MemoryDialog(notifier: widget.notifier, editMemory: _current),
    );
  }

  Future<void> _delete() async {
    final confirmed = await showDialog<bool>(
      context: context,
      useRootNavigator: true,
      builder: (_) => AlertDialog(
        title: const Text('Delete memory?'),
        content: const Text('This will also delete all attached photos.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.of(context).pop(true),
            style: ElevatedButton.styleFrom(
              backgroundColor: Theme.of(context).colorScheme.error,
              foregroundColor: Theme.of(context).colorScheme.onError,
            ),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await widget.notifier.deleteMemory(_current['id']?.toString() ?? '');
      if (mounted) Navigator.of(context, rootNavigator: true).pop();
    }
  }

  void _shareToSocial() {
    final pid = _current['public_id'] as String?;
    final notifier = widget.notifier;
    // Capture the (root) navigator's stable context, then close this modal and
    // open the share composer on top of the underlying screen.
    final navigator = Navigator.of(context, rootNavigator: true);
    final hostContext = navigator.context;
    navigator.pop();
    showSocialShareDialog(hostContext, notifier, initialMemoryPublicId: pid);
  }

  void _upgradePhotos() {
    final memory = _current;
    final notifier = widget.notifier;
    final navigator = Navigator.of(context, rootNavigator: true);
    final hostContext = navigator.context;
    navigator.pop();
    showPhotoUpgradeDialog(hostContext, notifier, memory);
  }

  String _photoThumbUrl(String uuid) {
    final id = _current['id']?.toString() ?? '';
    return widget.notifier.photoThumbUrl(id, uuid);
  }

  String _photoFullUrl(String uuid) {
    final id = _current['id']?.toString() ?? '';
    return widget.notifier.photoFullUrl(id, uuid);
  }

  Map<String, String> get _authHeaders => widget.notifier.photoAuthHeaders;

  @override
  Widget build(BuildContext context) {
    final photos = (_current['photos'] as List?)?.cast<String>() ?? [];
    final mems   = _allMemories;
    final idx    = _currentIndex;
    final hasPrev = idx > 0;
    final hasNext = idx < mems.length - 1;

    if (_photoViewerIndex >= 0) {
      return _PhotoViewer(
        photos: photos,
        initialIndex: _photoViewerIndex,
        fullUrlOf: _photoFullUrl,
        authHeaders: _authHeaders,
        onClose: () => setState(() => _photoViewerIndex = -1),
      );
    }

    return Dialog(
      backgroundColor: _kBg,
      insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 880, maxHeight: 700),
        child: LayoutBuilder(
          builder: (context, constraints) {
            final wide = constraints.maxWidth >= 640;
            return wide
                ? _desktopLayout(photos, mems, idx, hasPrev, hasNext)
                : _mobileLayout(photos, mems, idx, hasPrev, hasNext);
          },
        ),
      ),
    );
  }

  // ── Desktop layout: left mosaic + right content ────────────────────────────

  Widget _desktopLayout(
    List<String> photos,
    List<Map<String, dynamic>> mems,
    int idx,
    bool hasPrev,
    bool hasNext,
  ) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(16),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Expanded(child: _photoMosaic(photos)),
          Expanded(child: _contentPane(photos, mems, idx, hasPrev, hasNext)),
        ],
      ),
    );
  }

  // ── Mobile layout: hero on top + scrollable content ────────────────────────

  Widget _mobileLayout(
    List<String> photos,
    List<Map<String, dynamic>> mems,
    int idx,
    bool hasPrev,
    bool hasNext,
  ) {
    final name        = _displayName;
    final description = _displayDescription;
    final langs       = widget.notifier.styleFacet.languages;

    return ClipRRect(
      borderRadius: BorderRadius.circular(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Hero with overlaid title
          SizedBox(
            height: 240,
            child: Stack(
              fit: StackFit.expand,
              children: [
                if (photos.isNotEmpty)
                  Image.network(
                    _photoThumbUrl(photos[0]),
                    fit: BoxFit.cover,
                    headers: _authHeaders,
                  )
                else
                  Container(
                    color: _kBgDark,
                    child: const Center(
                      child: Icon(Icons.photo_camera_outlined,
                          size: 64, color: Color(0x44CBD5E1)),
                    ),
                  ),
                const Positioned.fill(
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      gradient: LinearGradient(
                        begin: Alignment.topCenter,
                        end: Alignment.bottomCenter,
                        colors: [Colors.transparent, _kBg],
                        stops: [0.4, 1.0],
                      ),
                    ),
                  ),
                ),
                Positioned(
                  top: 8, left: 8,
                  child: _dayBadge(_current['date'] as String?),
                ),
                Positioned(
                  top: 4, right: 4,
                  child: IconButton(
                    icon: const Icon(Icons.close, color: _kText2),
                    onPressed: () =>
                        Navigator.of(context, rootNavigator: true).pop(),
                  ),
                ),
                if (photos.length > 1)
                  Positioned(
                    top: 44, right: 8,
                    child: _photoCountChip(photos.length),
                  ),
                if (name != null && name.isNotEmpty)
                  Positioned(
                    bottom: 12, left: 16, right: 16,
                    child: Text(
                      name,
                      style: const TextStyle(
                        color: _kText1, fontSize: 18,
                        fontWeight: FontWeight.w700, letterSpacing: -0.3,
                      ),
                    ),
                  )
                else if (_nameUnavailable)
                  const Positioned(
                    bottom: 12, left: 16, right: 16,
                    child: Text(
                      'This memory is private',
                      style: TextStyle(
                        color: _kText2, fontSize: 15,
                        fontStyle: FontStyle.italic,
                      ),
                    ),
                  ),
              ],
            ),
          ),
          // Scrollable body
          Expanded(
            child: SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    _fmtDate(_current['date'] as String?).toUpperCase(),
                    style: const TextStyle(
                      color: _kOrange, fontFamily: 'monospace',
                      fontSize: 11, letterSpacing: 1.5,
                    ),
                  ),
                  if (langs.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    _languageBar(langs),
                  ],
                  if (description.isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text(description,
                        style: const TextStyle(
                            color: _kText2, fontSize: 15, height: 1.7)),
                  ] else if (_descriptionUnavailable) ...[
                    const SizedBox(height: 8),
                    const Text(
                      'Content is unavailable without the share key.',
                      style: TextStyle(
                          color: _kDim, fontSize: 13, fontStyle: FontStyle.italic),
                    ),
                  ],
                  if (photos.length > 1) ...[
                    const SizedBox(height: 12),
                    SizedBox(
                      height: 60,
                      child: ListView.separated(
                        scrollDirection: Axis.horizontal,
                        itemCount: photos.length,
                        separatorBuilder: (_, __) => const SizedBox(width: 6),
                        itemBuilder: (_, i) => GestureDetector(
                          onTap: () => setState(() => _photoViewerIndex = i),
                          child: ClipRRect(
                            borderRadius: BorderRadius.circular(6),
                            child: Image.network(
                              _photoThumbUrl(photos[i]),
                              width: 60, height: 60, fit: BoxFit.cover,
                              headers: _authHeaders,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                  const SizedBox(height: 16),
                  _CommentsSection(
                    comments: _comments,
                    loading: _commentsLoading,
                    canDelete: !widget.readOnly && widget.shareToken == null,
                    onDelete: _deleteComment,
                    onReply: (id, name) => setState(() {
                      _replyToId   = id;
                      _replyToName = name;
                    }),
                  ),
                  const SizedBox(height: 12),
                ],
              ),
            ),
          ),
          if (api.isAuthenticated) _commentInput(),
          _footerRow(),
        ],
      ),
    );
  }

  // ── Photo mosaic (desktop left pane) ──────────────────────────────────────

  Widget _photoMosaic(List<String> photos) {
    return Container(
      color: _kBgDark,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Expanded(
            child: Stack(
              fit: StackFit.expand,
              children: [
                if (photos.isNotEmpty)
                  Image.network(
                    _photoThumbUrl(photos[0]),
                    fit: BoxFit.cover,
                    headers: _authHeaders,
                  )
                else
                  const Center(
                    child: Icon(Icons.photo_camera_outlined,
                        size: 64, color: Color(0x44CBD5E1)),
                  ),
                Positioned(
                  top: 8, left: 8,
                  child: _dayBadge(_current['date'] as String?),
                ),
                if (photos.length > 1)
                  Positioned(
                    top: 8, right: 8,
                    child: _photoCountChip(photos.length),
                  ),
              ],
            ),
          ),
          if (photos.length > 1)
            Padding(
              padding: const EdgeInsets.all(8),
              child: Row(
                children: [
                  for (int i = 1; i <= 2 && i < photos.length; i++) ...[
                    if (i > 1) const SizedBox(width: 6),
                    Expanded(
                      child: GestureDetector(
                        onTap: () => setState(() => _photoViewerIndex = i),
                        child: ClipRRect(
                          borderRadius: BorderRadius.circular(6),
                          child: SizedBox(
                            height: 60,
                            child: Image.network(
                              _photoThumbUrl(photos[i]),
                              fit: BoxFit.cover,
                              headers: _authHeaders,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ],
              ),
            ),
        ],
      ),
    );
  }

  // ── Content pane (desktop right pane) ─────────────────────────────────────

  Widget _contentPane(
    List<String> photos,
    List<Map<String, dynamic>> mems,
    int idx,
    bool hasPrev,
    bool hasNext,
  ) {
    final theme       = Theme.of(context);
    final name        = _displayName;
    final dateStr     = _current['date'] as String?;
    final description = _displayDescription;
    final langs       = widget.notifier.styleFacet.languages;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 8, 8, 0),
          child: _navRow(mems, idx, hasPrev, hasNext),
        ),
        const SizedBox(height: 12),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20),
          child: Text(
            _fmtDate(dateStr).toUpperCase(),
            style: const TextStyle(
              color: _kOrange, fontFamily: 'monospace',
              fontSize: 11, letterSpacing: 1.5,
            ),
          ),
        ),
        const SizedBox(height: 6),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20),
          child: Text(
            (name != null && name.isNotEmpty)
                ? name
                : (_nameUnavailable ? 'This memory is private' : _fmtDate(dateStr)),
            style: theme.textTheme.headlineMedium?.copyWith(
              color: _kText1,
              fontWeight: FontWeight.w700,
              fontSize: 22,
              letterSpacing: -0.3,
              fontStyle: (name == null || name.isEmpty) && _nameUnavailable
                  ? FontStyle.italic
                  : FontStyle.normal,
            ),
          ),
        ),
        if (langs.isNotEmpty) _languageBar(langs),
        if (langs.isEmpty) const SizedBox(height: 12),
        Expanded(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (description.isNotEmpty) ...[
                  Text(
                    description,
                    style: const TextStyle(
                        color: _kText2, fontSize: 15, height: 1.7),
                  ),
                  const SizedBox(height: 16),
                ] else if (_descriptionUnavailable) ...[
                  const Text(
                    'Content is unavailable without the share key.',
                    style: TextStyle(
                        color: _kDim, fontSize: 13, fontStyle: FontStyle.italic),
                  ),
                  const SizedBox(height: 16),
                ],
                _CommentsSection(
                  comments: _comments,
                  loading: _commentsLoading,
                  canDelete: !widget.readOnly && widget.shareToken == null,
                  onDelete: _deleteComment,
                  onReply: (id, name) => setState(() {
                    _replyToId   = id;
                    _replyToName = name;
                  }),
                ),
                const SizedBox(height: 8),
              ],
            ),
          ),
        ),
        if (api.isAuthenticated) _commentInput(),
        _footerRow(),
      ],
    );
  }

  // ── Comment input bar ─────────────────────────────────────────────────────

  Widget _commentInput() {
    return Container(
      decoration: const BoxDecoration(
        color: _kBgDark,
        border: Border(top: BorderSide(color: _kBorder)),
      ),
      padding: const EdgeInsets.fromLTRB(12, 8, 8, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (_replyToName != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.reply, size: 14, color: _kDim),
                      const SizedBox(width: 4),
                      Text('Replying to $_replyToName',
                          style: const TextStyle(color: _kDim, fontSize: 12)),
                    ],
                  ),
                  GestureDetector(
                    onTap: () => setState(() {
                      _replyToId   = null;
                      _replyToName = null;
                    }),
                    child: const Icon(Icons.close, size: 14, color: _kDim),
                  ),
                ],
              ),
            ),
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: _commentCtrl,
                  style: const TextStyle(color: _kText1, fontSize: 14),
                  decoration: InputDecoration(
                    hintText: _replyToName != null
                        ? 'Write a reply…'
                        : 'Add a comment…',
                    hintStyle: const TextStyle(color: _kDim, fontSize: 14),
                    filled: true,
                    fillColor: _kBg,
                    contentPadding: const EdgeInsets.symmetric(
                        horizontal: 12, vertical: 8),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                      borderSide: const BorderSide(color: _kBorder),
                    ),
                    enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                      borderSide: const BorderSide(color: _kBorder),
                    ),
                    focusedBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                      borderSide: const BorderSide(color: _kBlue),
                    ),
                  ),
                  onSubmitted: (_) => _submitComment(),
                ),
              ),
              const SizedBox(width: 8),
              IconButton(
                icon: _submitting
                    ? const SizedBox(
                        width: 18, height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.send, color: _kBlue),
                onPressed: _submitting ? null : _submitComment,
              ),
            ],
          ),
        ],
      ),
    );
  }

  // ── Shared helpers ────────────────────────────────────────────────────────

  Widget _navRow(
      List<Map<String, dynamic>> mems, int idx, bool hasPrev, bool hasNext) {
    return Row(
      children: [
        IconButton(
          icon: const Icon(Icons.chevron_left, color: _kText2),
          visualDensity: VisualDensity.compact,
          onPressed: hasPrev ? () => _navigate(-1) : null,
        ),
        IconButton(
          icon: const Icon(Icons.chevron_right, color: _kText2),
          visualDensity: VisualDensity.compact,
          onPressed: hasNext ? () => _navigate(1) : null,
        ),
        if (mems.isNotEmpty)
          Text(
            '${idx + 1} / ${mems.length}',
            style: const TextStyle(color: _kDim, fontSize: 13),
          ),
        const Spacer(),
        IconButton(
          icon: const Icon(Icons.close, color: _kText2),
          visualDensity: VisualDensity.compact,
          onPressed: () => Navigator.of(context, rootNavigator: true).pop(),
        ),
      ],
    );
  }

  Widget _footerRow() {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: const BoxDecoration(
        color: _kBgDark,
        border: Border(top: BorderSide(color: _kBorder)),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          if (!widget.readOnly && widget.shareToken == null)
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: _kRed,
                side: const BorderSide(color: _kRed),
                minimumSize: const Size(0, 36),
              ),
              icon: const Icon(Icons.delete_outline, size: 16),
              label: const Text('Delete'),
              onPressed: _delete,
            )
          else
            const SizedBox.shrink(),
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (widget.shareToken == null) ...[
                IconButton(
                  icon: const Icon(Icons.share_outlined,
                      size: 18, color: _kText2),
                  tooltip: 'Share to social media',
                  onPressed: _shareToSocial,
                ),
                const SizedBox(width: 4),
              ],
              if (!widget.readOnly &&
                  widget.shareToken == null &&
                  (_current['photos'] as List?)?.isNotEmpty == true) ...[
                IconButton(
                  icon: const Icon(Icons.high_quality_outlined,
                      size: 18, color: _kText2),
                  tooltip: 'Upgrade photos',
                  onPressed: _upgradePhotos,
                ),
                const SizedBox(width: 4),
              ],
              _likesBar(),
              if (!widget.readOnly && widget.shareToken == null) ...[
                const SizedBox(width: 12),
                ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: _kBlue,
                    foregroundColor: Colors.white,
                    minimumSize: const Size(0, 36),
                  ),
                  icon: const Icon(Icons.edit_outlined, size: 16),
                  label: const Text('Edit'),
                  onPressed: _edit,
                ),
              ],
            ],
          ),
        ],
      ),
    );
  }

  Widget _likesBar() {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (_likeCount > 0)
          GestureDetector(
            onTap: _likers.isEmpty ? null : _showLikers,
            child: Text(
              '$_likeCount',
              style: TextStyle(
                color: _likers.isEmpty ? _kDim : _kText2,
                fontSize: 13,
                decoration:
                    _likers.isEmpty ? null : TextDecoration.underline,
              ),
            ),
          ),
        if (_likeCount > 0) const SizedBox(width: 4),
        Tooltip(
          message: _likers.isNotEmpty
              ? _likers.map((l) => l['name'] as String? ?? '').join('\n')
              : api.isAuthenticated
                  ? (_likedByMe ? 'Unlike' : 'Like')
                  : 'Sign in to like',
          preferBelow: false,
          child: IconButton(
            visualDensity: VisualDensity.compact,
            icon: Icon(
              _likedByMe ? Icons.favorite : Icons.favorite_border,
              color: _likedByMe ? _kRed : _kDim,
              size: 20,
            ),
            onPressed: api.isAuthenticated ? _toggleLike : null,
          ),
        ),
      ],
    );
  }

  void _showLikers() {
    showModalBottomSheet(
      context: context,
      backgroundColor: _kBgDark,
      builder: (_) => ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text('Liked by',
              style: TextStyle(color: _kText1, fontWeight: FontWeight.w600)),
          const SizedBox(height: 12),
          for (final l in _likers)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                children: [
                  const Icon(Icons.favorite, size: 14, color: _kRed),
                  const SizedBox(width: 8),
                  Text(l['name'] as String? ?? '',
                      style: const TextStyle(color: _kText2)),
                ],
              ),
            ),
        ],
      ),
    );
  }

  static Widget _dayBadge(String? dateStr) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: _kBgDark,
        border: Border.all(color: _kBorder),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Text(
        _fmtDate(dateStr),
        style: const TextStyle(
          color: _kText2, fontSize: 11, fontWeight: FontWeight.w500),
      ),
    );
  }

  static Widget _photoCountChip(int count) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: const Color(0xCC0C1722),
        border: Border.all(color: _kBorder),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.photo_library_outlined, size: 12, color: _kText2),
          const SizedBox(width: 4),
          Text('$count photos',
              style: const TextStyle(color: _kText2, fontSize: 11)),
        ],
      ),
    );
  }
}

// ── Comments section ──────────────────────────────────────────────────────────

class _CommentsSection extends StatelessWidget {
  final List<Map<String, dynamic>> comments;
  final bool loading;
  final bool canDelete;
  final void Function(int commentId) onDelete;
  final void Function(int commentId, String commenterName) onReply;

  const _CommentsSection({
    required this.comments,
    required this.loading,
    required this.canDelete,
    required this.onDelete,
    required this.onReply,
  });

  @override
  Widget build(BuildContext context) {
    if (loading) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 8),
        child: Center(child: SizedBox(
          width: 18, height: 18,
          child: CircularProgressIndicator(strokeWidth: 2))),
      );
    }
    if (comments.isEmpty) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 4),
        child: Text('No comments yet.',
            style: TextStyle(color: _kDim, fontSize: 13)),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '${_countAll(comments)} comment${_countAll(comments) == 1 ? '' : 's'}',
          style: const TextStyle(
              color: _kText2, fontWeight: FontWeight.w600, fontSize: 13),
        ),
        const SizedBox(height: 8),
        for (final c in comments)
          _CommentNode(
            comment: c,
            depth: 0,
            canDelete: canDelete,
            onDelete: onDelete,
            onReply: onReply,
          ),
      ],
    );
  }

  static int _countAll(List<Map<String, dynamic>> comments) {
    int count = 0;
    for (final c in comments) {
      count++;
      final replies = (c['replies'] as List?)?.cast<Map<String, dynamic>>() ?? [];
      count += _countAll(replies);
    }
    return count;
  }
}

class _CommentNode extends StatelessWidget {
  final Map<String, dynamic> comment;
  final int depth;
  final bool canDelete;
  final void Function(int commentId) onDelete;
  final void Function(int commentId, String commenterName) onReply;

  const _CommentNode({
    required this.comment,
    required this.depth,
    required this.canDelete,
    required this.onDelete,
    required this.onReply,
  });

  @override
  Widget build(BuildContext context) {
    final id      = (comment['id'] as num?)?.toInt() ?? 0;
    final name    = comment['commenter_name'] as String? ?? '';
    final text    = comment['text'] as String? ?? '';
    final created = comment['created_at'] as String? ?? '';
    final replies = (comment['replies'] as List?)
        ?.cast<Map<String, dynamic>>() ?? [];

    final timeLabel = _relativeTime(created);

    return Padding(
      padding: EdgeInsets.only(left: depth * 16.0, bottom: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Avatar initial
              Container(
                width: 28, height: 28,
                decoration: BoxDecoration(
                  color: _kBorder,
                  shape: BoxShape.circle,
                ),
                alignment: Alignment.center,
                child: Text(
                  name.isNotEmpty ? name[0].toUpperCase() : '?',
                  style: const TextStyle(
                      color: _kText1, fontSize: 12, fontWeight: FontWeight.w700),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Text(name,
                            style: const TextStyle(
                                color: _kText1,
                                fontSize: 13,
                                fontWeight: FontWeight.w600)),
                        const SizedBox(width: 6),
                        Text(timeLabel,
                            style: const TextStyle(
                                color: _kDim, fontSize: 11)),
                      ],
                    ),
                    const SizedBox(height: 2),
                    Text(text,
                        style: const TextStyle(
                            color: _kText2, fontSize: 13, height: 1.5)),
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        if (api.isAuthenticated)
                          GestureDetector(
                            onTap: () => onReply(id, name),
                            child: const Text('Reply',
                                style: TextStyle(
                                    color: _kDim,
                                    fontSize: 12,
                                    fontWeight: FontWeight.w500)),
                          ),
                        if (canDelete) ...[
                          if (api.isAuthenticated) const SizedBox(width: 12),
                          GestureDetector(
                            onTap: () => onDelete(id),
                            child: const Text('Delete',
                                style: TextStyle(
                                    color: _kRed,
                                    fontSize: 12,
                                    fontWeight: FontWeight.w500)),
                          ),
                        ],
                      ],
                    ),
                  ],
                ),
              ),
            ],
          ),
          if (replies.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Column(
                children: replies
                    .map((r) => _CommentNode(
                          comment: r,
                          depth: depth + 1,
                          canDelete: canDelete,
                          onDelete: onDelete,
                          onReply: onReply,
                        ))
                    .toList(),
              ),
            ),
        ],
      ),
    );
  }

  static String _relativeTime(String iso) {
    final dt = DateTime.tryParse(iso);
    if (dt == null) return '';
    final diff = DateTime.now().toUtc().difference(dt.toUtc());
    if (diff.inSeconds < 60) return 'just now';
    if (diff.inMinutes < 60) return '${diff.inMinutes}m ago';
    if (diff.inHours < 24)   return '${diff.inHours}h ago';
    if (diff.inDays < 30)    return '${diff.inDays}d ago';
    return '${(diff.inDays / 30).floor()}mo ago';
  }
}

// ── Full-screen photo viewer ──────────────────────────────────────────────────

class _PhotoViewer extends StatefulWidget {
  final List<String> photos;
  final int initialIndex;
  final String Function(String uuid) fullUrlOf;
  final Map<String, String> authHeaders;
  final VoidCallback onClose;

  const _PhotoViewer({
    required this.photos,
    required this.initialIndex,
    required this.fullUrlOf,
    required this.authHeaders,
    required this.onClose,
  });

  @override
  State<_PhotoViewer> createState() => _PhotoViewerState();
}

class _PhotoViewerState extends State<_PhotoViewer> {
  late final PageController _page;

  @override
  void initState() {
    super.initState();
    _page = PageController(initialPage: widget.initialIndex);
  }

  @override
  void dispose() {
    _page.dispose();
    super.dispose();
  }

  int get _current => _page.hasClients
      ? (_page.page?.round() ?? widget.initialIndex)
      : widget.initialIndex;

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: Colors.black,
      insetPadding: EdgeInsets.zero,
      child: SizedBox.expand(
        child: Stack(
          children: [
            PageView.builder(
              controller: _page,
              itemCount: widget.photos.length,
              onPageChanged: (_) => setState(() {}),
              itemBuilder: (_, i) => Image.network(
                widget.fullUrlOf(widget.photos[i]),
                fit: BoxFit.contain,
                headers: widget.authHeaders,
              ),
            ),
            Positioned(
              top: 16, right: 16,
              child: IconButton(
                icon: const Icon(Icons.close, color: Colors.white),
                onPressed: widget.onClose,
              ),
            ),
            if (widget.photos.length > 1)
              Positioned(
                left: 8, top: 0, bottom: 0,
                child: Center(
                  child: IconButton(
                    icon: const Icon(Icons.chevron_left,
                        color: Colors.white, size: 40),
                    onPressed: _current > 0
                        ? () => _page.previousPage(
                              duration: const Duration(milliseconds: 200),
                              curve: Curves.easeInOut,
                            )
                        : null,
                  ),
                ),
              ),
            if (widget.photos.length > 1)
              Positioned(
                right: 8, top: 0, bottom: 0,
                child: Center(
                  child: IconButton(
                    icon: const Icon(Icons.chevron_right,
                        color: Colors.white, size: 40),
                    onPressed: _current < widget.photos.length - 1
                        ? () => _page.nextPage(
                              duration: const Duration(milliseconds: 200),
                              curve: Curves.easeInOut,
                            )
                        : null,
                  ),
                ),
              ),
            Positioned(
              bottom: 16, left: 0, right: 0,
              child: Center(
                child: Text(
                  '${_current + 1} / ${widget.photos.length}',
                  style: const TextStyle(color: Colors.white70),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Language flag button ───────────────────────────────────────────────────────

class _LangButton extends StatelessWidget {
  final String label;
  final bool active;
  final bool loading;
  final VoidCallback onTap;

  const _LangButton({
    required this.label,
    required this.active,
    required this.loading,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: loading ? null : onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: active ? const Color(0x2060A5FA) : Colors.transparent,
          borderRadius: BorderRadius.circular(6),
          border: Border.all(
            color: active ? const Color(0xFF60A5FA) : const Color(0xFF1F2F42),
          ),
        ),
        child: loading
            ? const SizedBox(
                width: 16, height: 16,
                child: CircularProgressIndicator(
                    strokeWidth: 1.5, color: Color(0xFF60A5FA)))
            : Text(label,
                style: TextStyle(
                  fontSize: 16,
                  color: active
                      ? const Color(0xFF60A5FA)
                      : const Color(0xFFCBD5E1),
                )),
      ),
    );
  }
}
