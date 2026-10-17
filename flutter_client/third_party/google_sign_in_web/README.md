# google_sign_in_web 1.1.3, patched

A copy of [`google_sign_in_web` 1.1.3](https://pub.dev/packages/google_sign_in_web/versions/1.1.3)
(BSD-3-Clause, see `LICENSE`), wired in through `dependency_overrides` in
`flutter_client/pubspec.yaml`.

## The patch (#611)

Upstream's `GoogleSignInPlugin` constructor starts loading
`https://accounts.google.com/gsi/client`. The constructor runs from the web
plugin registrant, before `main()`, so every page of the web app fetched
Google's script, share links included, whether or not the visitor signs in.

Here the load starts in `init()` instead, and the app calls
`GoogleSignIn.instance.initialize()` only from the login screen
(`lib/src/auth/google_sign_in_init.dart`). Every other plugin API already
waits for `init()`, so nothing else changes.

The patch is the second commit that touched this folder; the first is the
verbatim upstream copy, so `git log -p -- flutter_client/third_party/google_sign_in_web`
shows exactly what differs.

Reported upstream as flutter/flutter#194640.

## Upgrading

Drop this folder and the `dependency_overrides` entry once an upstream release
loads the SDK in `init()`. Until then, to take a new upstream version: copy it
here in one commit, re-apply the patch in the next, and run
`flutter test --platform chrome` in this folder.
