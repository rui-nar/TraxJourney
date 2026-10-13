/// Web: adds a `<script>` to the page's head and waits for it to run.
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart' as web;

Future<void> injectScript(String src) {
  final done = Completer<void>();
  final script = web.HTMLScriptElement()
    ..src = src
    ..async = true;
  script.addEventListener(
      'load',
      ((web.Event _) {
        if (!done.isCompleted) done.complete();
      }).toJS);
  script.addEventListener(
      'error',
      ((web.Event _) {
        // Removed so a retry adds a fresh element rather than a second copy.
        script.remove();
        if (!done.isCompleted) {
          done.completeError(StateError('failed to load $src'));
        }
      }).toJS);
  web.document.head!.appendChild(script);
  return done.future;
}
