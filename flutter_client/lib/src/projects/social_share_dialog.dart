/// Compose-and-share UI for posting a memory to social media.
///
/// [SocialShareModal] holds form state only; all decision logic lives in
/// [SocialShareController] and its injected edges, so the widget is testable
/// with fakes (no [ProjectNotifier] dependency). The OS share sheet ("Share…")
/// is the primary action — it can carry the map image + photos; WhatsApp /
/// Facebook are text+link shortcuts and "Copy link" puts the deep link on the
/// clipboard.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import '../share/share_asset_source_impl.dart';
import '../share/share_capabilities.dart';
import '../share/share_interfaces.dart';
import '../share/share_link_resolver_impl.dart';
import '../share/share_strategy.dart';
import '../share/share_transport_impl.dart';
import '../share/social_share_controller.dart';
import 'project_notifier.dart';
import '../crypto/encrypted_display.dart';

/// Builds the sorted memory list and opens the share modal — a bottom sheet on
/// narrow/phone widths, a centered dialog on wider screens.
void showSocialShareDialog(
  BuildContext context,
  ProjectNotifier notifier, {
  String? initialMemoryPublicId,
}) {
  final memories = notifier.itemsFacet.items
      .where((i) => i['item_type'] == 'memory' && i['memory'] != null)
      .map((i) => (i['memory'] as Map).cast<String, dynamic>())
      .toList()
    ..sort((a, b) =>
        (a['date'] as String? ?? '').compareTo(b['date'] as String? ?? ''));

  if (memories.isEmpty) {
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Add a memory first to share it.')),
    );
    return;
  }

  final caps = createShareCapabilities();
  final controller = SocialShareController(
    assets: ShareAssetSourceImpl(notifier, () => context),
    links: ShareLinkResolverImpl(notifier),
    transport: const ShareTransportImpl(),
    caps: caps,
  );

  SocialShareModal modal({required bool sheet}) => SocialShareModal(
        allMemories: memories,
        initialMemoryPublicId: initialMemoryPublicId,
        controller: controller,
        caps: caps,
        thumbUrl: (memId, uuid) => notifier.photoThumbUrl(memId, uuid),
        authHeaders: notifier.photoAuthHeaders,
        sheet: sheet,
      );

  final size = MediaQuery.of(context).size;
  final useSheet = size.width < 600;

  if (useSheet) {
    final theme = Theme.of(context);
    showModalBottomSheet<void>(
      context: context,
      useRootNavigator: true,
      isScrollControlled: true,
      backgroundColor: theme.colorScheme.surface,
      barrierColor: Colors.black.withValues(alpha: 0.46),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(22)),
      ),
      builder: (sheetCtx) => Padding(
        padding: EdgeInsets.only(
            bottom: MediaQuery.of(sheetCtx).viewInsets.bottom),
        child: SafeArea(
          top: false,
          child: ConstrainedBox(
            constraints:
                BoxConstraints(maxHeight: size.height * 0.94),
            child: modal(sheet: true),
          ),
        ),
      ),
    );
  } else {
    showDialog<void>(
      context: context,
      barrierColor: Colors.black.withValues(alpha: 0.46),
      builder: (_) => Dialog(
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        child: ConstrainedBox(
          constraints:
              BoxConstraints(maxWidth: 460, maxHeight: size.height - 80),
          child: modal(sheet: false),
        ),
      ),
    );
  }
}

/// The full share-memory modal chrome. Hosted in a [Dialog] (wide) or a
/// bottom sheet (narrow); see [showSocialShareDialog].
class SocialShareModal extends StatefulWidget {
  final List<Map<String, dynamic>> allMemories;
  final String? initialMemoryPublicId;
  final SocialShareController controller;
  final ShareCapabilities caps;

  /// Resolves a thumbnail URL for `(memoryId, photoUuid)`.
  final String Function(String memoryId, String photoUuid) thumbUrl;
  final Map<String, String> authHeaders;

  /// When true, render the bottom-sheet affordances (grab handle, roomy footer).
  final bool sheet;

  const SocialShareModal({
    super.key,
    required this.allMemories,
    required this.controller,
    required this.caps,
    required this.thumbUrl,
    required this.authHeaders,
    this.initialMemoryPublicId,
    this.sheet = false,
  });

  @override
  State<SocialShareModal> createState() => _SocialShareModalState();
}

class _SocialShareModalState extends State<SocialShareModal> {
  static const _maxLen = 2000;

  late Map<String, dynamic> _memory;
  bool _dayFocus = false;
  bool _includeMap = true;
  bool _includeLink = true;
  final _selectedPhotos = <String>{};
  final _textCtrl = TextEditingController();
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _memory = widget.allMemories.firstWhere(
      (m) => m['public_id'] == widget.initialMemoryPublicId,
      orElse: () => widget.allMemories.last,
    );
    _syncMemorySelection();
    _textCtrl.addListener(() => setState(() {})); // live char counter
  }

  void _syncMemorySelection() {
    _selectedPhotos
      ..clear()
      ..addAll(_photosOf(_memory));
    _textCtrl.text = readableOrNull(_memory['description'] as String?) ?? '';
  }

  List<String> _photosOf(Map<String, dynamic> m) =>
      (m['photos'] as List?)?.cast<String>() ?? const [];

  @override
  void dispose() {
    _textCtrl.dispose();
    super.dispose();
  }

  String _memoryLabel(Map<String, dynamic> m) {
    final date = m['date'] as String? ?? '';
    final name = readableOrNull(m['name'] as String?)?.trim();
    return (name == null || name.isEmpty) ? date : '$date · $name';
  }

  Future<void> _share(ShareTarget target) async {
    setState(() => _busy = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      await widget.controller.share(
        target: target,
        memoryId: (_memory['id'] as num).toInt(),
        memoryPublicId: _memory['public_id'] as String? ?? '',
        memoryDate: _memory['date'] as String?,
        customText: _textCtrl.text,
        includeLink: _includeLink,
        includeMap: _includeMap,
        dayFocus: _dayFocus,
        selectedPhotoUuids: _selectedPhotos.toList(),
      );

      if (target == ShareTarget.copyLink) {
        messenger.showSnackBar(
          const SnackBar(content: Text('Link copied to clipboard')),
        );
      } else {
        // Honest feedback when the platform couldn't attach images.
        final wantedFiles = _includeMap || _selectedPhotos.isNotEmpty;
        if (target == ShareTarget.system &&
            !widget.caps.canShareFiles &&
            wantedFiles) {
          messenger.showSnackBar(const SnackBar(
            content: Text(
                "Images can't be attached here — shared text + link instead."),
          ));
        }
      }
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('Share failed: $e')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (widget.sheet) const _GrabHandle(),
        _buildHeader(context),
        Flexible(
          child: SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
            child: _buildBody(context),
          ),
        ),
        _buildShareTo(context),
        _buildFooter(context),
      ],
    );
  }

  Widget _buildHeader(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 16, 12, 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Share memory',
                    style: theme.textTheme.titleLarge
                        ?.copyWith(fontWeight: FontWeight.w700)),
                const SizedBox(height: 2),
                Text('Pick what to include, then where to send it',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
              ],
            ),
          ),
          IconButton(
            onPressed: _busy ? null : () => Navigator.of(context).pop(),
            icon: const Icon(Icons.close),
            tooltip: 'Close',
          ),
        ],
      ),
    );
  }

  Widget _buildBody(BuildContext context) {
    final photos = _photosOf(_memory);
    final selectedCount =
        photos.where(_selectedPhotos.contains).length;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _Eyebrow('Memory'),
        _MemorySelect(
          label: _memoryLabel(_memory),
          date: _memory['date'] as String? ?? '',
          title: readableOrNull(_memory['name'] as String?)?.trim().isNotEmpty == true
              ? _memory['name'] as String
              : (_memory['date'] as String? ?? 'Memory'),
          memories: widget.allMemories,
          currentPublicId: _memory['public_id'] as String?,
          labelOf: _memoryLabel,
          enabled: !_busy && widget.allMemories.length > 1,
          onSelected: (pid) {
            final m = widget.allMemories
                .firstWhere((m) => m['public_id'] == pid, orElse: () => _memory);
            setState(() {
              _memory = m;
              _syncMemorySelection();
            });
          },
        ),

        const _Eyebrow('Include'),
        _ToggleList(children: [
          _ToggleRow(
            icon: Icons.map_outlined,
            label: 'Trip map',
            desc: 'Adds the full route to the share image',
            value: _includeMap,
            onChanged:
                _busy ? null : (v) => setState(() => _includeMap = v),
          ),
          _ToggleRow(
            label: 'Zoom to this day',
            desc: 'Frame just this leg instead of the whole trip',
            value: _dayFocus,
            nested: true,
            enabled: _includeMap && !_busy,
            onChanged: (v) => setState(() => _dayFocus = v),
          ),
          _ToggleRow(
            icon: Icons.link,
            label: 'Link to memory',
            desc: _includeLink
                ? 'A private link to this memory will be included'
                : "Recipients won't get a link",
            value: _includeLink,
            onChanged:
                _busy ? null : (v) => setState(() => _includeLink = v),
          ),
        ]),

        if (photos.isNotEmpty) ...[
          _Eyebrow('Photos · $selectedCount of ${photos.length}'),
          _PhotoStrip(
            photos: photos,
            isSelected: _selectedPhotos.contains,
            thumbUrl: (uuid) =>
                widget.thumbUrl(_memory['id'].toString(), uuid),
            headers: widget.authHeaders,
            onToggle: _busy
                ? null
                : (uuid) => setState(() {
                      if (!_selectedPhotos.remove(uuid)) {
                        _selectedPhotos.add(uuid);
                      }
                    }),
          ),
        ],

        const _Eyebrow('Message'),
        _MessageField(
          controller: _textCtrl,
          enabled: !_busy,
          maxLen: _maxLen,
        ),
      ],
    );
  }

  Widget _buildShareTo(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(20, 4, 20, 0),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: theme.colorScheme.outlineVariant)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const _Eyebrow('Share to'),
          Row(
            children: [
              Expanded(
                child: _ShareTargetButton(
                  badgeColor: kWhatsApp,
                  icon: Icons.chat,
                  label: 'WhatsApp',
                  onTap: _busy ? null : () => _share(ShareTarget.whatsapp),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _ShareTargetButton(
                  badgeColor: kFacebookBlue,
                  glyph: 'f',
                  label: 'Facebook',
                  onTap: _busy ? null : () => _share(ShareTarget.facebook),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _ShareTargetButton(
                  badgeColor: theme.colorScheme.surfaceContainerHighest,
                  badgeFg: theme.colorScheme.onSurfaceVariant,
                  icon: Icons.content_copy,
                  label: 'Copy link',
                  onTap: _busy ? null : () => _share(ShareTarget.copyLink),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildFooter(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 12, 20, widget.sheet ? 24 : 14),
      child: Row(
        children: [
          if (_busy) ...[
            const SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(strokeWidth: 2)),
            const SizedBox(width: 10),
            const Text('Preparing…'),
          ],
          const Spacer(),
          TextButton(
            onPressed: _busy ? null : () => Navigator.of(context).pop(),
            child: const Text('Cancel'),
          ),
          const SizedBox(width: 8),
          _ShareCta(
            onTap: _busy ? null : () => _share(ShareTarget.system),
          ),
        ],
      ),
    );
  }
}

// ── Sub-widgets ──────────────────────────────────────────────────────────────

class _GrabHandle extends StatelessWidget {
  const _GrabHandle();

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 38,
      height: 4,
      margin: const EdgeInsets.only(top: 9),
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.outline.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(999),
      ),
    );
  }
}

class _Eyebrow extends StatelessWidget {
  final String text;
  const _Eyebrow(this.text);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(0, 14, 0, 6),
      child: Text(
        text.toUpperCase(),
        style: TextStyle(
          fontSize: 10,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.9,
          color: Theme.of(context).colorScheme.onSurfaceVariant,
        ),
      ),
    );
  }
}

class _MemorySelect extends StatelessWidget {
  final String label;
  final String date;
  final String title;
  final List<Map<String, dynamic>> memories;
  final String? currentPublicId;
  final String Function(Map<String, dynamic>) labelOf;
  final bool enabled;
  final ValueChanged<String?> onSelected;

  const _MemorySelect({
    required this.label,
    required this.date,
    required this.title,
    required this.memories,
    required this.currentPublicId,
    required this.labelOf,
    required this.enabled,
    required this.onSelected,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cs = theme.colorScheme;

    final field = Container(
      height: 48,
      padding: const EdgeInsets.symmetric(horizontal: 10),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest.withValues(alpha: 0.4),
        border: Border.all(color: cs.outlineVariant),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          Container(
            width: 28,
            height: 28,
            decoration: BoxDecoration(
              color: cs.primary.withValues(alpha: 0.12),
              borderRadius: BorderRadius.circular(7),
            ),
            child: Icon(Icons.calendar_today, size: 16, color: cs.primary),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(date,
                    style: monoStyle(
                        fontSize: 12, color: cs.onSurfaceVariant)),
                Text(title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.bodyMedium
                        ?.copyWith(fontWeight: FontWeight.w600)),
              ],
            ),
          ),
          if (enabled)
            Icon(Icons.expand_more, color: cs.onSurfaceVariant),
        ],
      ),
    );

    if (!enabled) return field;

    return PopupMenuButton<String>(
      tooltip: 'Choose memory',
      initialValue: currentPublicId,
      onSelected: onSelected,
      position: PopupMenuPosition.under,
      constraints: const BoxConstraints(maxWidth: 420),
      itemBuilder: (_) => [
        for (final m in memories)
          PopupMenuItem<String>(
            value: m['public_id'] as String?,
            child: Text(labelOf(m), overflow: TextOverflow.ellipsis),
          ),
      ],
      child: field,
    );
  }
}

class _ToggleList extends StatelessWidget {
  final List<Widget> children;
  const _ToggleList({required this.children});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final rows = <Widget>[];
    for (var i = 0; i < children.length; i++) {
      if (i > 0) rows.add(Divider(height: 1, color: cs.outlineVariant));
      rows.add(children[i]);
    }
    return Container(
      decoration: BoxDecoration(
        border: Border.all(color: cs.outlineVariant),
        borderRadius: BorderRadius.circular(12),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(children: rows),
    );
  }
}

class _ToggleRow extends StatelessWidget {
  final IconData? icon;
  final String label;
  final String desc;
  final bool value;
  final bool nested;
  final bool enabled;
  final ValueChanged<bool>? onChanged;

  const _ToggleRow({
    required this.label,
    required this.desc,
    required this.value,
    this.icon,
    this.nested = false,
    this.enabled = true,
    this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cs = theme.colorScheme;
    final off = onChanged == null || !enabled;

    final row = Container(
      color: nested ? cs.surfaceContainerHighest.withValues(alpha: 0.4) : null,
      padding: EdgeInsets.fromLTRB(nested ? 48 : 12, 9, 12, 9),
      child: Row(
        children: [
          if (icon != null) ...[
            Container(
              width: 30,
              height: 30,
              decoration: BoxDecoration(
                color: cs.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Icon(icon, size: 19, color: cs.onSurfaceVariant),
            ),
            const SizedBox(width: 12),
          ],
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label,
                    style: theme.textTheme.bodyMedium?.copyWith(
                        fontWeight:
                            nested ? FontWeight.w500 : FontWeight.w600)),
                const SizedBox(height: 1),
                Text(desc,
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: cs.onSurfaceVariant)),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Switch(value: value, onChanged: off ? null : onChanged),
        ],
      ),
    );

    // Nested row stays visible but inert when its parent toggle is off.
    return Opacity(opacity: enabled ? 1 : 0.45, child: row);
  }
}

class _PhotoStrip extends StatelessWidget {
  final List<String> photos;
  final bool Function(String uuid) isSelected;
  final String Function(String uuid) thumbUrl;
  final Map<String, String> headers;
  final ValueChanged<String>? onToggle;

  const _PhotoStrip({
    required this.photos,
    required this.isSelected,
    required this.thumbUrl,
    required this.headers,
    required this.onToggle,
  });

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Row(
        children: [
          for (final uuid in photos)
            Padding(
              padding: const EdgeInsets.only(right: 8),
              child: _PhotoChip(
                key: ValueKey('share_photo_$uuid'),
                url: thumbUrl(uuid),
                headers: headers,
                selected: isSelected(uuid),
                onTap: onToggle == null ? null : () => onToggle!(uuid),
              ),
            ),
        ],
      ),
    );
  }
}

class _PhotoChip extends StatelessWidget {
  final String url;
  final Map<String, String> headers;
  final bool selected;
  final VoidCallback? onTap;

  const _PhotoChip({
    super.key,
    required this.url,
    required this.headers,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return GestureDetector(
      onTap: onTap,
      child: Container(
        width: 62,
        height: 62,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(10),
          border: Border.all(
            color: selected ? cs.primary : cs.outlineVariant,
            width: selected ? 2 : 1,
          ),
          boxShadow: selected
              ? [BoxShadow(color: cs.primary.withValues(alpha: 0.5), blurRadius: 0, spreadRadius: 1)]
              : null,
        ),
        clipBehavior: Clip.antiAlias,
        child: Stack(
          fit: StackFit.expand,
          children: [
            Image.network(
              url,
              headers: headers,
              fit: BoxFit.cover,
              errorBuilder: (_, __, ___) => Container(
                color: cs.surfaceContainerHighest,
                child: Icon(Icons.image_outlined,
                    size: 22, color: cs.onSurfaceVariant.withValues(alpha: 0.7)),
              ),
            ),
            if (!selected)
              Container(color: cs.surface.withValues(alpha: 0.5)),
            Positioned(
              top: 5,
              right: 5,
              child: Container(
                width: 20,
                height: 20,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: selected ? cs.primary : cs.surface,
                  border: selected
                      ? null
                      : Border.all(color: cs.outline, width: 1.5),
                ),
                child: Icon(
                  Icons.check,
                  size: 14,
                  color: selected ? Colors.white : Colors.transparent,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _MessageField extends StatelessWidget {
  final TextEditingController controller;
  final bool enabled;
  final int maxLen;

  const _MessageField({
    required this.controller,
    required this.enabled,
    required this.maxLen,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Stack(
      children: [
        TextField(
          controller: controller,
          enabled: enabled,
          minLines: 3,
          maxLines: 6,
          maxLength: maxLen,
          buildCounter: (_,
                  {required currentLength,
                  required isFocused,
                  maxLength}) =>
              null, // hide default counter; custom one below
          decoration: InputDecoration(
            filled: true,
            fillColor: cs.surfaceContainerHighest.withValues(alpha: 0.4),
            contentPadding: const EdgeInsets.fromLTRB(12, 10, 12, 24),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: BorderSide(color: cs.outlineVariant),
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: BorderSide(color: cs.outlineVariant),
            ),
          ),
        ),
        Positioned(
          right: 12,
          bottom: 10,
          child: Text(
            '${controller.text.characters.length} / $maxLen',
            style: monoStyle(
                fontSize: 11,
                color: cs.onSurfaceVariant.withValues(alpha: 0.8)),
          ),
        ),
      ],
    );
  }
}

class _ShareTargetButton extends StatelessWidget {
  final Color badgeColor;
  final Color? badgeFg;
  final IconData? icon;
  final String? glyph;
  final String label;
  final VoidCallback? onTap;

  const _ShareTargetButton({
    required this.badgeColor,
    required this.label,
    required this.onTap,
    this.icon,
    this.glyph,
    this.badgeFg,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cs = theme.colorScheme;
    final fg = badgeFg ?? Colors.white;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(12),
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 10),
        decoration: BoxDecoration(
          color: cs.surfaceContainerHighest.withValues(alpha: 0.4),
          border: Border.all(color: cs.outlineVariant),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Column(
          children: [
            Container(
              width: 34,
              height: 34,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: badgeColor,
              ),
              child: glyph != null
                  ? Text(glyph!,
                      style: TextStyle(
                          color: fg,
                          fontWeight: FontWeight.w800,
                          fontFamily: 'Georgia',
                          fontSize: 18))
                  : Icon(icon, size: 20, color: fg),
            ),
            const SizedBox(height: 6),
            Text(label,
                style: theme.textTheme.bodySmall
                    ?.copyWith(fontWeight: FontWeight.w600)),
          ],
        ),
      ),
    );
  }
}

class _ShareCta extends StatelessWidget {
  final VoidCallback? onTap;
  const _ShareCta({required this.onTap});

  @override
  Widget build(BuildContext context) {
    final brightness = Theme.of(context).brightness;
    final enabled = onTap != null;
    return Opacity(
      opacity: enabled ? 1 : 0.6,
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: metallicBlue(brightness),
          borderRadius: BorderRadius.circular(10),
          boxShadow: enabled ? kShadow2(brightness) : null,
        ),
        child: Material(
          color: Colors.transparent,
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(10),
            child: const Padding(
              padding: EdgeInsets.symmetric(horizontal: 20, vertical: 10),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.ios_share, size: 18, color: Colors.white),
                  SizedBox(width: 8),
                  Text('Share…',
                      style: TextStyle(
                          color: Colors.white, fontWeight: FontWeight.w600)),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
