"""Cache-Control policy for the served Flutter web build.

Regression guard for the stale-bundle class of bug: Flutter does not
content-hash its entry-point filenames, so they must never be long-cached or a
returning user keeps running an old build (e.g. one with a wrong baked API URL)
after a deploy.
"""
from api.router import _cache_control_for, _NO_CACHE, _LONG_CACHE


def test_app_entry_points_are_never_long_cached():
    for path in [
        "main.dart.js",
        "flutter.js",
        "flutter_bootstrap.js",
        "flutter_service_worker.js",
        "index.html",
        "manifest.json",
        "version.json",
        "main.dart.js_1.part.js",  # deferred chunk — also unhashed
    ]:
        assert _cache_control_for(path) == _NO_CACHE, path


def test_static_trees_are_cacheable():
    assert _cache_control_for("assets/AssetManifest.json") == _LONG_CACHE
    assert _cache_control_for("assets/fonts/MaterialIcons-Regular.otf") == _LONG_CACHE
    assert _cache_control_for("canvaskit/canvaskit.wasm") == _LONG_CACHE
    assert _cache_control_for("canvaskit/canvaskit.js") == _LONG_CACHE
    # The engine's mirrored fallback fonts: versioned paths (#601, #602).
    assert _cache_control_for(
        "fallback-fonts/roboto/v32/KFOmCnqEu92Fr1Me4GZLCzYlKw.woff2") == _LONG_CACHE


def test_unknown_root_files_default_to_no_cache():
    assert _cache_control_for("favicon.png") == _NO_CACHE
    assert _cache_control_for("") == _NO_CACHE
