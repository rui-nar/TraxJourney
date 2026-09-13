"""The /metrics scrape endpoint and its HTTP instrumentation (issue #125).

Exercised against the real ``api.router.app`` — route ordering and the
instrumentator wiring are precisely what these tests are protecting.
"""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from api.metrics import router as metrics_router


@pytest.fixture(scope="module")
def client():
    import api.router as router
    return TestClient(router.app)


def _scrape(client, token="right"):
    return client.get("/metrics", headers={"Authorization": f"Bearer {token}"})


class TestMetricsAuth:
    def test_disabled_when_no_token_configured(self, client, monkeypatch):
        """Secure by default: Caddy proxies every path of the public domain to
        this app, so a deployment that never sets METRICS_TOKEN must not
        publish its internals just because the code shipped."""
        monkeypatch.delenv("METRICS_TOKEN", raising=False)
        assert client.get("/metrics").status_code == 404

    def test_rejects_a_wrong_token(self, client, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "right")
        assert _scrape(client, "wrong").status_code == 401

    def test_rejects_a_missing_header(self, client, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "right")
        assert client.get("/metrics").status_code == 401

    def test_rejects_a_non_bearer_scheme(self, client, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "right")
        assert client.get(
            "/metrics", headers={"Authorization": "Basic right"}
        ).status_code == 401

    def test_serves_prometheus_text_with_the_right_token(self, client, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "right")
        client.get("/api/version")  # the HTTP metrics appear on first request
        resp = _scrape(client)

        assert resp.status_code == 200
        assert resp.headers["content-type"] == CONTENT_TYPE_LATEST
        assert "# HELP traxjourney_http_requests_total" in resp.text

    def test_token_is_read_per_request(self, client, monkeypatch):
        """Read from the environment at request time, so enabling metrics on a
        running deployment doesn't need a rebuilt image."""
        monkeypatch.delenv("METRICS_TOKEN", raising=False)
        assert client.get("/metrics").status_code == 404
        monkeypatch.setenv("METRICS_TOKEN", "later")
        assert _scrape(client, "later").status_code == 200


class TestHttpInstrumentation:
    def test_requests_are_counted_under_the_route_template(self, client, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "right")
        client.get("/api/projects/alpha")
        client.get("/api/projects/beta")

        body = _scrape(client).text

        # The path parameter is templated, so two distinct projects — and every
        # project name a user ever invents — share a single time series.
        assert 'handler="/api/projects/{name}"' in body
        assert 'handler="/api/projects/alpha"' not in body
        assert "traxjourney_http_requests_total" in body

    def test_status_codes_are_not_grouped(self, client, monkeypatch):
        """409 (optimistic-lock conflict) and 401-vs-404 are individually
        actionable; a `4xx` bucket would hide exactly what we watch for."""
        monkeypatch.setenv("METRICS_TOKEN", "right")
        client.get("/api/projects/alpha")  # 401 — no bearer token

        body = _scrape(client).text
        assert 'status="401"' in body
        assert 'status="4xx"' not in body

    def test_inprogress_gauge_is_exposed(self, client, monkeypatch):
        """Distinguishing a slow request from a stuck one is what issue #45's
        investigation lacked."""
        monkeypatch.setenv("METRICS_TOKEN", "right")
        assert "traxjourney_http_requests_inprogress" in _scrape(client).text

    def test_metrics_endpoint_excludes_itself(self, client, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "right")
        _scrape(client)
        assert 'handler="/metrics"' not in _scrape(client).text


class TestReloadSafety:
    def test_reloading_the_router_does_not_duplicate_collectors(self):
        """prometheus_client refuses to register a metric name twice, and
        tests/test_version_endpoint.py reloads api.router to re-read
        APP_VERSION — so instrumentation has to survive a module reload."""
        import api.router as router

        importlib.reload(router)
        importlib.reload(router)  # twice, to be sure the cleanup is repeatable


class TestRouteOrdering:
    def test_spa_fallback_does_not_swallow_metrics(self, monkeypatch, tmp_path):
        """api/router.py registers a `/{full_path:path}` catch-all that serves
        the Flutter web build. Starlette matches routes in registration order,
        so /metrics must be included BEFORE it — otherwise a scrape silently
        gets index.html with a 200 and Prometheus records nothing useful.

        Built standalone because the SPA route only exists in api.router when a
        web_client/ build is present, which it isn't in CI."""
        app = FastAPI()
        app.include_router(metrics_router)

        (tmp_path / "index.html").write_text("<html>spa</html>")

        @app.get("/{full_path:path}", include_in_schema=False)
        def _spa(full_path: str):
            return FileResponse(tmp_path / "index.html")

        monkeypatch.setenv("METRICS_TOKEN", "right")
        resp = _scrape(TestClient(app))

        assert resp.status_code == 200
        assert "spa" not in resp.text
        assert resp.headers["content-type"].startswith("text/plain")
