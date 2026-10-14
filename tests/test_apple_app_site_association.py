"""Apple app-site-association file for iOS Universal Links (store plan D14).

Same failure mode as assetlinks.json: the SPA catch-all answers every unmatched
path with index.html and a 200, which iOS records as a failed association.
Exercised against the real ``api.router.app`` — route ordering is the point.
"""

import importlib
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "flutter_client" / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
URL = "/.well-known/apple-app-site-association"
TEAM = "ABCDE12345"


@pytest.fixture
def client():
    import api.router as router
    return TestClient(router.app)


def test_serves_the_exact_association(client, monkeypatch):
    monkeypatch.setenv("APPLE_TEAM_ID", TEAM)
    response = client.get(URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "applinks": {
            "details": [
                {
                    "appIDs": [f"{TEAM}.com.traxjourney.app"],
                    "components": [
                        {"/": "/share/*"},
                        {"/": "/join/*"},
                        {"/": "/verify-email/*"},
                    ],
                }
            ]
        }
    }


def test_does_not_redirect(client, monkeypatch):
    monkeypatch.setenv("APPLE_TEAM_ID", TEAM)
    response = client.get(URL, follow_redirects=False)

    assert response.status_code == 200


@pytest.mark.parametrize("value", ["", "   "])
def test_404s_when_unconfigured(client, monkeypatch, value):
    monkeypatch.setenv("APPLE_TEAM_ID", value)
    response = client.get(URL)

    assert response.status_code == 404
    # Specifically not the SPA fallback's index.html with a 200.
    assert response.headers["content-type"].startswith("application/json")


def test_404s_when_the_variable_is_absent(client, monkeypatch):
    monkeypatch.delenv("APPLE_TEAM_ID", raising=False)

    assert client.get(URL).status_code == 404


def test_paths_match_the_android_manifest(client, monkeypatch):
    """The two platforms must claim the same URLs."""
    monkeypatch.setenv("APPLE_TEAM_ID", TEAM)
    components = client.get(URL).json()["applinks"]["details"][0]["components"]
    served = sorted(c["/"] for c in components)

    manifest = MANIFEST.read_text(encoding="utf-8")
    prefixes = re.findall(
        r'android:scheme="https"\s+android:host="traxjourney\.com"\s+android:pathPrefix="([^"]+)"',
        manifest,
    )
    assert prefixes, "no https App Links found in AndroidManifest.xml"
    assert served == sorted(f"{p}/*" for p in prefixes)


def test_not_shadowed_by_the_spa_fallback(monkeypatch, tmp_path):
    """Reload the router with a web build present, as production runs it."""
    import api.router as router

    (tmp_path / "index.html").write_text("<!doctype html><html><body>spa shell</body></html>")
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
    monkeypatch.setattr(router, "_web_dir", str(tmp_path))
    assert any(getattr(r, "path", None) == "/{full_path:path}" for r in router.app.routes)
    try:
        monkeypatch.setenv("APPLE_TEAM_ID", TEAM)
        response = TestClient(router.app).get(URL)

        assert response.status_code == 200
        assert "spa shell" not in response.text
        assert response.json()["applinks"]["details"][0]["appIDs"] == [f"{TEAM}.com.traxjourney.app"]
    finally:
        monkeypatch.undo()
        importlib.reload(router)
