"""The web build's catch-all route serves only files inside the web build.

``api/router.py`` serves ``/{full_path:path}`` from the Flutter web build and
falls back to ``index.html`` for client-side routes. A path that resolves
outside the build directory, by any spelling, must be a plain 404: never a file
from elsewhere on disk, and not the SPA shell either.

Most cases are driven through a raw ASGI request built the way uvicorn builds
one (``path`` percent-decoded, ``raw_path`` as sent), because an HTTP client
normalises ``..`` segments away before the request leaves it and the handler
would never see them.
"""

import asyncio
import importlib
import os
import sys
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

_INDEX = "<!doctype html><html><body>spa shell</body></html>"
_SENTINEL = "outside-the-web-build"


@pytest.fixture
def site(monkeypatch, tmp_path):
    """The real app with a temporary web build and files beside it."""
    import api.router as router

    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text(_INDEX)
    (web / "main.dart.js").write_text("// bundle")
    (web / "assets" / "data.json").write_text('{"asset": true}')
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text(_SENTINEL)
    # A sibling whose name starts with the build directory's name.
    (tmp_path / "web-other").mkdir()
    (tmp_path / "web-other" / "sentinel.txt").write_text(_SENTINEL)

    expected_web_dir = os.path.normpath(
        os.path.join(os.path.dirname(router.__file__), "..", "web_client")
    )
    real_isdir = os.path.isdir

    def _isdir(path):
        if os.path.normpath(str(path)) == expected_web_dir:
            return True
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _isdir)
    importlib.reload(router)
    monkeypatch.setattr(os.path, "isdir", real_isdir)
    monkeypatch.setattr(router, "_web_dir", str(web))
    assert any(getattr(r, "path", None) == "/{full_path:path}" for r in router.app.routes), (
        "SPA catch-all was not registered; the fixture no longer reproduces production"
    )
    try:
        yield router.app, web, sentinel
    finally:
        monkeypatch.undo()
        importlib.reload(router)


def _raw_get(app, raw_path: str):
    """Send one GET straight into the ASGI app, bypassing URL normalisation.

    ``raw_path`` is what goes on the wire; ``path`` is its percent-decoded form,
    exactly as uvicorn fills the scope.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, body.decode("utf-8", "replace")


def _assert_refused(app, raw_path):
    status, _headers, body = _raw_get(app, raw_path)
    assert _SENTINEL not in body, raw_path
    assert "spa shell" not in body, raw_path
    assert status == 404, (raw_path, status)


def _url_path(p) -> str:
    """An absolute filesystem path as a URL path: /tmp/x or C:/Users/x."""
    return str(p).replace("\\", "/")


# ── Paths that leave the build directory ─────────────────────────────────────

def test_dot_dot_segment_is_refused(site):
    app, _web, _sentinel = site
    _assert_refused(app, "/../sentinel.txt")


def test_percent_encoded_dots_are_refused(site):
    app, _web, _sentinel = site
    _assert_refused(app, "/%2e%2e/sentinel.txt")
    _assert_refused(app, "/%2E%2E/sentinel.txt")


def test_percent_encoded_slash_is_refused(site):
    app, _web, _sentinel = site
    _assert_refused(app, "/..%2fsentinel.txt")
    _assert_refused(app, "/..%2Fsentinel.txt")


def test_nested_dot_dot_through_a_real_directory_is_refused(site):
    app, _web, _sentinel = site
    _assert_refused(app, "/assets/../../sentinel.txt")
    _assert_refused(app, "/assets/%2e%2e/%2e%2e/sentinel.txt")


def test_absolute_path_after_double_slash_is_refused(site):
    """``//<abs>`` (POSIX) or ``/C:/...`` (Windows) makes the tail absolute."""
    app, _web, sentinel = site
    _assert_refused(app, "/" + _url_path(sentinel))


def test_percent_encoded_absolute_path_is_refused(site):
    app, _web, sentinel = site
    _assert_refused(app, "/" + quote(_url_path(sentinel), safe=""))


def test_sibling_directory_sharing_the_name_prefix_is_refused(site):
    app, _web, _sentinel = site
    _assert_refused(app, "/../web-other/sentinel.txt")


def test_backslash_separators_never_leave_the_build(site):
    """On Windows ``\\`` is a separator; elsewhere it is part of a name."""
    app, _web, _sentinel = site
    for raw in ("/..%5Csentinel.txt", "/assets%5C..%5C..%5Csentinel.txt"):
        status, _headers, body = _raw_get(app, raw)
        assert _SENTINEL not in body, raw
        if sys.platform == "win32":
            assert status == 404, raw


def test_embedded_nul_is_not_a_server_error(site):
    """realpath raises on NUL on POSIX; the handler must not turn that into a 500."""
    app, _web, _sentinel = site
    status, _headers, body = _raw_get(app, "/main.dart.js%00")
    assert status in (200, 404)
    assert body != "// bundle"


def _symlink_or_skip(link, target, target_is_directory=False):
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks not permitted here: {exc}")


def test_symlinked_file_pointing_outside_is_refused(site):
    app, web, sentinel = site
    _symlink_or_skip(web / "assets" / "outside.txt", sentinel)
    _assert_refused(app, "/assets/outside.txt")


def test_symlinked_directory_pointing_outside_is_refused(site):
    app, web, sentinel = site
    _symlink_or_skip(web / "outside", sentinel.parent, target_is_directory=True)
    _assert_refused(app, "/outside/sentinel.txt")


def test_symlink_inside_the_build_is_still_served(site):
    """realpath is not a blanket symlink ban: a link that stays inside works."""
    app, web, _sentinel = site
    _symlink_or_skip(web / "alias.js", web / "main.dart.js")
    status, headers, body = _raw_get(app, "/alias.js")
    assert status == 200
    assert body == "// bundle"
    assert headers["cache-control"] == "no-cache"


def test_web_dir_that_is_itself_a_symlink_still_serves_the_build(site, monkeypatch, tmp_path):
    """A deploy may point web_client at the real build through a symlink."""
    import api.router as router

    app, web, _sentinel = site
    link = tmp_path / "web_link"
    _symlink_or_skip(link, web, target_is_directory=True)
    monkeypatch.setattr(router, "_web_dir", str(link))

    status, headers, body = _raw_get(app, "/main.dart.js")
    assert status == 200
    assert body == "// bundle"
    assert headers["cache-control"] == "no-cache"

    status, headers, body = _raw_get(app, "/assets/data.json")
    assert status == 200
    assert headers["cache-control"] == "public, max-age=86400"

    status, _headers, body = _raw_get(app, "/trips/foo")
    assert status == 200
    assert body == _INDEX


def test_http_client_spellings_are_refused(site):
    """The same spellings sent through a normal HTTP client."""
    app, _web, sentinel = site
    client = TestClient(app)
    for url in (
        "/../sentinel.txt",
        "/%2e%2e/sentinel.txt",
        "/..%2fsentinel.txt",
        "/assets/../../sentinel.txt",
        "/" + _url_path(sentinel),
        "/" + quote(_url_path(sentinel), safe=""),
    ):
        resp = client.get(url)
        assert _SENTINEL not in resp.text, url


# ── Legitimate requests keep working ─────────────────────────────────────────

def test_entry_point_is_served_no_cache(site):
    app, _web, _sentinel = site
    resp = TestClient(app).get("/main.dart.js")
    assert resp.status_code == 200
    assert resp.text == "// bundle"
    assert resp.headers["cache-control"] == "no-cache"


def test_asset_is_served_with_long_cache(site):
    app, _web, _sentinel = site
    resp = TestClient(app).get("/assets/data.json")
    assert resp.status_code == 200
    assert resp.json() == {"asset": True}
    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_dot_dot_that_stays_inside_is_served_under_the_real_files_policy(site):
    """assets/../main.dart.js is main.dart.js, so it must not be long-cached."""
    app, _web, _sentinel = site
    status, headers, body = _raw_get(app, "/assets/../main.dart.js")
    assert status == 200
    assert body == "// bundle"
    assert headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/", "/trips/foo", "/trips/foo/day/3", "/assets"])
def test_client_routes_get_the_spa_shell(site, path):
    app, _web, _sentinel = site
    resp = TestClient(app).get(path)
    assert resp.status_code == 200
    assert resp.text == _INDEX
    assert resp.headers["cache-control"] == "no-cache"


def test_unknown_api_path_is_still_a_404(site):
    app, _web, _sentinel = site
    resp = TestClient(app).get("/api/nope")
    assert resp.status_code == 404
    assert "spa shell" not in resp.text
