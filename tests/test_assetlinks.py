"""Digital Asset Links statement for Android App Links (issue #136).

This one small JSON file is the whole difference between a share link opening in
the app and opening in a browser. It is also easy to break without noticing: the
SPA catch-all route answers *every* unmatched path with index.html, so a
mis-registered route here returns HTML with a 200 and Android just records a
failed verification.

Exercised against the real ``api.router.app`` — route ordering is the point.
"""

import pytest
from fastapi.testclient import TestClient

from api.router import ANDROID_PACKAGE_NAME

FINGERPRINT = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
OTHER = "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11"


@pytest.fixture(scope="module")
def client():
    import api.router as router
    return TestClient(router.app)


def test_serves_the_configured_fingerprint(client, monkeypatch):
    monkeypatch.setenv("ANDROID_CERT_FINGERPRINTS", FINGERPRINT)
    response = client.get("/.well-known/assetlinks.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    statement = response.json()
    assert len(statement) == 1
    target = statement[0]["target"]
    assert statement[0]["relation"] == ["delegate_permission/common.handle_all_urls"]
    assert target["namespace"] == "android_app"
    assert target["package_name"] == "com.traxjourney.app"
    assert target["sha256_cert_fingerprints"] == [FINGERPRINT]


def test_registered_ahead_of_the_spa_catch_all():
    """The catch-all only exists when web_client/ is built (not in CI), so assert
    on the route table rather than waiting for a deploy to show the problem."""
    import api.router as router

    paths = [getattr(route, "path", None) for route in router.app.routes]
    assert "/.well-known/assetlinks.json" in paths, "no explicit route — the SPA fallback would answer with index.html"
    catch_alls = [i for i, path in enumerate(paths) if path == "/{full_path:spa_path}"]
    if catch_alls:
        assert paths.index("/.well-known/assetlinks.json") < catch_alls[0]


def test_package_matches_the_gradle_application_id():
    """A mismatch verifies nothing, with no error anywhere to show for it."""
    gradle = (
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "flutter_client" / "android" / "app" / "build.gradle.kts"
    ).read_text(encoding="utf-8")
    assert f'applicationId = "{ANDROID_PACKAGE_NAME}"' in gradle


def test_several_fingerprints_for_a_key_rotation(client, monkeypatch):
    monkeypatch.setenv("ANDROID_CERT_FINGERPRINTS", f"{FINGERPRINT}, {OTHER}")
    fingerprints = client.get("/.well-known/assetlinks.json").json()[0]["target"][
        "sha256_cert_fingerprints"
    ]
    assert fingerprints == [FINGERPRINT, OTHER]


def test_lowercase_fingerprints_are_normalised(client, monkeypatch):
    """Copied out of a keytool listing or a console, case varies; Android's
    comparison does not."""
    monkeypatch.setenv("ANDROID_CERT_FINGERPRINTS", FINGERPRINT.lower())
    fingerprints = client.get("/.well-known/assetlinks.json").json()[0]["target"][
        "sha256_cert_fingerprints"
    ]
    assert fingerprints == [FINGERPRINT]


@pytest.mark.parametrize("value", ["", "   ", ",,"])
def test_404s_when_unconfigured(client, monkeypatch, value):
    """Better than an empty statement: Android caches a failed verification, so
    publishing one before the fingerprint is known poisons every install made in
    the meantime."""
    monkeypatch.setenv("ANDROID_CERT_FINGERPRINTS", value)
    response = client.get("/.well-known/assetlinks.json")

    assert response.status_code == 404
    # Specifically not the SPA fallback's index.html with a 200.
    assert response.headers["content-type"].startswith("application/json")
