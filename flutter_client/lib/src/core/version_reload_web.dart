import 'package:web/web.dart' as web;

/// Hard-reload the page so the browser fetches the freshly-deployed bundle.
void reloadApp() => web.window.location.reload();
