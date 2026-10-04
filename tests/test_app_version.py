"""/api/version serves the minimum supported client version (Decision 15).

The client compares its own build with ``min_client_version`` and blocks itself
below it. "0.0.0" is the default and means off.
"""
import importlib

from fastapi.testclient import TestClient


def _get_version(monkeypatch, **env):
    import api.router as router
    for key in ("APP_VERSION", "MIN_CLIENT_VERSION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    importlib.reload(router)
    try:
        return TestClient(router.app).get("/api/version")
    finally:
        # Reload with the env cleared so other tests see the defaults.
        for key in env:
            monkeypatch.delenv(key, raising=False)
        importlib.reload(router)


def test_version_serves_both_keys_with_defaults(monkeypatch):
    resp = _get_version(monkeypatch)
    assert resp.status_code == 200
    assert resp.json() == {"version": "dev", "min_client_version": "0.0.0"}


def test_min_client_version_env_override(monkeypatch):
    resp = _get_version(monkeypatch, APP_VERSION="v1.2.3", MIN_CLIENT_VERSION="1.1.0")
    assert resp.status_code == 200
    assert resp.json() == {"version": "v1.2.3", "min_client_version": "1.1.0"}
