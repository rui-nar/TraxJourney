/// Links to the privacy policy and terms of service (issue #421).
///
/// The pages themselves are HTML served by the server (`legal/` in the repo,
/// routed in api/router.py), so Google's OAuth verification and the Play
/// Console can read them without running the app. Nothing here repeats their
/// text.
library;

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api/client.dart';

/// Server paths of the legal pages.
const kPrivacyPolicyPath = '/privacy';
const kTermsOfServicePath = '/terms';

/// The full URL of a legal page on the server this app is connected to.
///
/// Built from the server rather than hard-coding traxjourney.com: whoever runs
/// a server is the one whose policy applies to its users, so a self-hosted
/// server serves its own pages. The web app talks to its own origin
/// ([ApiClient.baseUrl] is empty), so the page's origin is that server;
/// Android and iOS always carry the server address.
Uri legalPageUri(String path, {String? apiBaseUrl, Uri? pageBase}) {
  final base = apiBaseUrl ?? api.baseUrl;
  if (base.isEmpty) return (pageBase ?? Uri.base).resolve(path);
  return Uri.parse('$base$path');
}

/// Opens a legal page in the browser, outside the app.
Future<void> openLegalPage(String path) =>
    launchUrl(legalPageUri(path), mode: LaunchMode.externalApplication);

/// "By [action] you agree to the Terms of Service and have read the Privacy
/// Policy." with both names as links, for the sign-in and sign-up screens.
class LegalNotice extends StatelessWidget {
  /// What the user is doing, e.g. "creating an account".
  final String action;

  const LegalNotice({super.key, required this.action});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final style = theme.textTheme.bodySmall?.copyWith(
      color: theme.colorScheme.onSurfaceVariant,
    );
    return Wrap(
      alignment: WrapAlignment.center,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: [
        Text('By $action you agree to the ', style: style),
        LegalLink(
            label: 'Terms of Service', path: kTermsOfServicePath, style: style),
        Text(' and have read the ', style: style),
        LegalLink(
            label: 'Privacy Policy', path: kPrivacyPolicyPath, style: style),
        Text('.', style: style),
      ],
    );
  }
}

/// A compact text link to one legal page, sized to sit inside running text.
class LegalLink extends StatelessWidget {
  final String label;
  final String path;
  final TextStyle? style;

  const LegalLink({
    super.key,
    required this.label,
    required this.path,
    this.style,
  });

  @override
  Widget build(BuildContext context) {
    return TextButton(
      style: TextButton.styleFrom(
        padding: EdgeInsets.zero,
        minimumSize: Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        visualDensity: VisualDensity.compact,
        textStyle: style,
      ),
      onPressed: () => openLegalPage(path),
      child: Text(label),
    );
  }
}
