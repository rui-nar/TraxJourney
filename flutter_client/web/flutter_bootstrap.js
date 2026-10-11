{{flutter_js}}
{{flutter_build_config}}

// The web engine downloads its fallback fonts (Roboto at start, Noto for
// emoji and scripts the bundled fonts lack) from fonts.gstatic.com unless told
// otherwise. tool/mirror_fallback_fonts.dart copies them into the build's
// fallback-fonts/, and this points the engine there, so no font request
// reaches Google (#601, #602). Resolved against the document base, not the
// page, so a deep link such as /share/<token> still finds them at the root.
_flutter.loader.load({
  serviceWorkerSettings: {
    serviceWorkerVersion: {{flutter_service_worker_version}},
  },
  config: {
    fontFallbackBaseUrl: new URL('fallback-fonts/', document.baseURI).href,
  },
});
