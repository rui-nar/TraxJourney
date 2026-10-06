"""The /api/version probe the web client uses to detect a stale cached bundle."""
import importlib
import logging

from fastapi.testclient import TestClient


def _client():
    import api.router as router
    return TestClient(router.app)


def test_version_defaults_to_dev_without_env(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    import api.router as router
    importlib.reload(router)
    resp = TestClient(router.app).get("/api/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "dev", "min_client_version": "0.0.0"}


def test_version_reports_baked_app_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v9.9.9")
    import api.router as router
    importlib.reload(router)
    try:
        resp = TestClient(router.app).get("/api/version")
        assert resp.status_code == 200
        assert resp.json() == {"version": "v9.9.9", "min_client_version": "0.0.0"}
    finally:
        # Reload once more with the env cleared so other tests see the default.
        monkeypatch.delenv("APP_VERSION", raising=False)
        importlib.reload(router)


def test_min_client_version_env_override(monkeypatch):
    """Decision 15: the client gate's minimum, "0.0.0" (off) unless overridden."""
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setenv("MIN_CLIENT_VERSION", "1.1.0")
    import api.router as router
    importlib.reload(router)
    try:
        resp = TestClient(router.app).get("/api/version")
        assert resp.status_code == 200
        assert resp.json() == {"version": "dev", "min_client_version": "1.1.0"}
    finally:
        monkeypatch.delenv("MIN_CLIENT_VERSION", raising=False)
        importlib.reload(router)


def test_startup_logs_running_version(monkeypatch, caplog):
    """Issue #179: reading a log file must tell you which build produced it."""
    monkeypatch.setenv("APP_VERSION", "v9.9.9")
    import api.router as router
    try:
        with caplog.at_level(logging.INFO, logger="api.router"):
            importlib.reload(router)
        assert "v9.9.9" in caplog.text
    finally:
        monkeypatch.delenv("APP_VERSION", raising=False)
        importlib.reload(router)
