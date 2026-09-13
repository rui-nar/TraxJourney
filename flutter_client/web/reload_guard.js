// Android PWA black-screen recovery.
//
// Flutter web (CanvasKit renderer) paints through a WebGL context. On Android
// Chrome, when an installed PWA is backgrounded, the OS/Chromium can reclaim
// that context; Flutter's own restore logic is unreliable and often leaves
// the canvas permanently black until a full reload. This is a long-standing
// engine issue (flutter/flutter#164879, #75914, #50721) with no build-flag
// fix, so we recover with a plain reload from outside the engine.
//
// A lost context is usually followed by a normal `webglcontextrestored` once
// the page is visible again — that's the common case and isn't a bug, so we
// give it a grace period to happen before reloading. Only when restoration
// doesn't land in time (or, on devices that never fire these events at all,
// after an implausibly long backgrounding) do we force a reload.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.__traxJourneyReloadGuard = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function createReloadGuard(opts) {
    opts = opts || {};
    var win = opts.window || (typeof window !== 'undefined' ? window : undefined);
    var doc = opts.document || (win && win.document);
    var reload = opts.reload || function () { win.location.reload(); };
    var now = opts.now || function () { return Date.now(); };
    // Grace period given to Flutter's own restore after a context loss before
    // we give up and reload.
    var restoreGraceMs = opts.restoreGraceMs != null ? opts.restoreGraceMs : 3000;
    // Last-resort threshold for devices that never fire webglcontextlost at
    // all yet still end up with a dead canvas. Deliberately long: this is a
    // blind time-based guess with no way to confirm the canvas is actually
    // broken, so it should only catch implausibly long backgrounding, not
    // ordinary app-switching.
    var hiddenFallbackMs = opts.hiddenFallbackMs != null ? opts.hiddenFallbackMs : 30 * 60 * 1000;
    var scanIntervalMs = opts.scanIntervalMs != null ? opts.scanIntervalMs : 2000;

    var reloadTimer = null;
    var contextLost = false;

    function scheduleReload() {
      contextLost = true;
      if (reloadTimer) return;
      reloadTimer = win.setTimeout(function () {
        reloadTimer = null;
        if (contextLost) reload();
      }, restoreGraceMs);
    }

    function cancelReload() {
      contextLost = false;
      if (reloadTimer) {
        win.clearTimeout(reloadTimer);
        reloadTimer = null;
      }
    }

    var instrumentedCanvases = typeof WeakSet !== 'undefined' ? new WeakSet() : null;

    function instrument(canvas) {
      if (instrumentedCanvases) {
        if (instrumentedCanvases.has(canvas)) return;
        instrumentedCanvases.add(canvas);
      }
      // No preventDefault(): leave Flutter's own restore attempt alone, this
      // listener is only a backstop for when that attempt doesn't work.
      canvas.addEventListener('webglcontextlost', scheduleReload);
      canvas.addEventListener('webglcontextrestored', cancelReload);
    }

    function collectCanvases(root, out) {
      root.querySelectorAll('canvas').forEach(function (canvas) { out.push(canvas); });
      // Flutter's host elements (flutter-view / flt-glass-pane) may attach a
      // shadow root, so descend into any we find too.
      root.querySelectorAll('*').forEach(function (el) {
        if (el.shadowRoot) collectCanvases(el.shadowRoot, out);
      });
    }

    // Poll instead of a one-shot query: Flutter can create/replace canvas
    // elements (and shadow roots) throughout the page's lifetime.
    var scanTimer = win.setInterval(function () {
      var canvases = [];
      collectCanvases(doc, canvases);
      canvases.forEach(instrument);
    }, scanIntervalMs);

    var hiddenAt = null;
    function onVisibilityChange() {
      if (doc.hidden) {
        hiddenAt = now();
      } else if (hiddenAt !== null) {
        var hiddenMs = now() - hiddenAt;
        hiddenAt = null;
        if (hiddenMs >= hiddenFallbackMs) scheduleReload();
      }
    }
    doc.addEventListener('visibilitychange', onVisibilityChange);

    return {
      stop: function () {
        win.clearInterval(scanTimer);
        doc.removeEventListener('visibilitychange', onVisibilityChange);
        cancelReload();
      },
    };
  }

  return { createReloadGuard: createReloadGuard };
});
