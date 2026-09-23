/// The two version numbers the app can show: its own, baked in at build time,
/// and the server's, as reported by `GET /api/version` (issue #275).
///
/// Every place that shows a version shows both, in the same shape, so a bug
/// report never has to guess which half is which — a native app is routinely a
/// release or two behind the server it talks to, and a stale cached web bundle
/// is the whole reason [VersionGate] exists.
library;

import 'package:flutter/material.dart';

/// Set by `--dart-define=APP_VERSION` at build time; already carries its `v`
/// prefix from the release tag, so it is shown verbatim.
const kClientVersion =
    String.fromEnvironment('APP_VERSION', defaultValue: 'dev');

/// The server version last reported by `/api/version`.
///
/// `null` until a call succeeds — and it may legitimately stay null for the
/// whole session: offline, a server that is down, or a first frame that beats
/// the request home. Readers must render that case immediately rather than
/// wait, which is what [versionLabel] does. `VersionGate` is the sole writer;
/// nothing else should fetch the endpoint.
final ValueNotifier<String?> serverVersion = ValueNotifier<String?>(null);

/// One line naming both versions, e.g. `app v0.42.0 · server v0.42.0`.
///
/// An absent [server] reads as `server unknown` rather than collapsing to the
/// app version alone: on a diagnostic surface a blank where the server version
/// should be would read as "there is no server", and a placeholder that spins
/// would never resolve offline. The two being equal is not called out — the
/// stale-bundle bar owns that story, and repeating it here would either
/// duplicate or contradict it.
///
/// [omitUnknownServer] drops the clause entirely instead, for the splash
/// screen — see [VersionText.omitUnknownServer].
String versionLabel(String client, String? server,
    {bool omitUnknownServer = false}) {
  final known = server != null && server.isNotEmpty;
  if (!known && omitUnknownServer) return 'app $client';
  return 'app $client · server ${known ? server : 'unknown'}';
}

/// [versionLabel] for the running build, re-rendered when the server version
/// arrives. [prefix] is prepended verbatim (e.g. `'© 2026 TraxJourney · '`).
class VersionText extends StatelessWidget {
  const VersionText({
    super.key,
    this.prefix = '',
    this.style,
    this.textAlign,
    this.omitUnknownServer = false,
  });

  final String prefix;
  final TextStyle? style;
  final TextAlign? textAlign;

  /// Show only the app version while the server's is unknown, rather than
  /// `server unknown`.
  ///
  /// For the splash screen alone. Everywhere else the label is a diagnostic
  /// readout, where "we could not reach the server" is information worth
  /// stating. The splash is a brand moment that lasts a few hundred
  /// milliseconds — long enough for `/api/version` to land mid-display, so the
  /// honest label would visibly flip from `server unknown` to a version while
  /// the user watches. Omitting the clause until it is known is not a
  /// misleading blank: nothing claims a server version is missing, the clause
  /// simply appears when there is one to show.
  final bool omitUnknownServer;

  @override
  Widget build(BuildContext context) => ValueListenableBuilder<String?>(
        valueListenable: serverVersion,
        builder: (_, server, __) => Text(
          '$prefix${versionLabel(kClientVersion, server, omitUnknownServer: omitUnknownServer)}',
          style: style,
          textAlign: textAlign,
        ),
      );
}
