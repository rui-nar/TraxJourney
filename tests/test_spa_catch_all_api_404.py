"""The Flutter-web catch-all must not answer unknown /api/... paths (issue #151).

``api/router.py`` registers ``/{full_path:path}`` last and serves ``index.html``
for anything it doesn't recognise, so client-side routes deep-link correctly.
Before this guard it did the same for ``/api/...``: a client calling an endpoint
that no longer exists (``/export-viewtrip``, since renamed to ``/export-traxj``)
got the web app's HTML with a 200, saved it as a "backup" and reported success.

Exercised against the real ``api.router.app`` because the route order and the
app's own 404 handler are what's under test. The SPA route is only registered
when a ``web_client/`` build is present, which it never is under pytest, so the
router is reloaded with that directory reported as present and then pointed at a
temporary build.
"""

import importlib
import os

import pytest
from fastapi.testclient import TestClient

_INDEX = "<!doctype html><html><body>spa shell</body></html>"


@pytest.fixture
def client(monkeypatch, tmp_path):
    import api.router as router

    (tmp_path / "index.html").write_text(_INDEX)
    (tmp_path / "main.dart.js").write_text("// bundle")

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
    # spa_fallback reads the module global at request time.
    monkeypatch.setattr(router, "_web_dir", str(tmp_path))
    assert any(getattr(r, "path", None) == "/{full_path:path}" for r in router.app.routes), (
        "SPA catch-all was not registered; the fixture no longer reproduces production"
    )
    try:
        yield TestClient(router.app)
    finally:
        # Reload without the fake build so later tests see the usual app.
        monkeypatch.undo()
        importlib.reload(router)


def test_unknown_api_path_is_a_json_404(client):
    resp = client.get("/api/nope")

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"] == "Not Found"
    assert "spa shell" not in resp.text


def test_removed_export_route_is_a_404_not_the_web_app(client):
    """The concrete failure: an old client downloading a renamed export route."""
    resp = client.get("/api/projects/Trip/export-no-such-format")

    assert resp.status_code == 404
    assert "spa shell" not in resp.text


def test_bare_api_prefix_is_a_404(client):
    assert client.get("/api").status_code == 404


def test_client_route_still_serves_the_web_app(client):
    resp = client.get("/some/client/route")

    assert resp.status_code == 200
    assert resp.text == _INDEX
    assert resp.headers["content-type"].startswith("text/html")


def test_path_merely_starting_with_api_is_a_client_route(client):
    resp = client.get("/apiary")

    assert resp.status_code == 200
    assert resp.text == _INDEX


def test_static_asset_still_served(client):
    resp = client.get("/main.dart.js")

    assert resp.status_code == 200
    assert resp.text == "// bundle"


def test_real_api_route_still_reachable(client):
    resp = client.get("/api/version")

    assert resp.status_code == 200
    assert "version" in resp.json()
