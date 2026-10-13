/// Non-web builds never load Apple JS: iOS uses the native sheet and Android
/// a Custom Tab.
library;

Future<void> injectScript(String src) =>
    Future.error(UnsupportedError('Apple JS is only loaded on the web'));
