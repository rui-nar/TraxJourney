"""Login and registration counters (issue #125).

Assertions are always on the *delta* around the call under test: the metric
registry is process-wide and counters only ever climb, so an absolute value
would depend on whatever else ran earlier in the session.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import models.db as db_module
from api.auth import router as auth_router


@pytest.fixture
def engine(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)
    return test_engine


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(auth_router)
    return TestClient(app)


def _register(client, username="rider@example.com", password="s3cret"):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password,
              "first_name": "Test", "last_name": "Rider"},
    )


class TestPasswordAuthCounters:
    def test_registration_is_counted(self, client, metric):
        before = metric("traxjourney_registrations_total", provider="password")
        assert _register(client).status_code == 201
        assert metric("traxjourney_registrations_total", provider="password") - before == 1

    def test_successful_login_is_counted(self, client, metric):
        _register(client)
        before = metric("traxjourney_logins_total", provider="password", result="success")

        resp = client.post(
            "/api/auth/token",
            json={"username": "rider@example.com", "password": "s3cret"},
        )

        assert resp.status_code == 200
        assert metric("traxjourney_logins_total",
                      provider="password", result="success") - before == 1

    def test_wrong_password_is_counted_as_a_failure(self, client, metric):
        _register(client)
        before = metric("traxjourney_logins_total", provider="password", result="failure")
        ok_before = metric("traxjourney_logins_total", provider="password", result="success")

        resp = client.post(
            "/api/auth/token",
            json={"username": "rider@example.com", "password": "wrong"},
        )

        assert resp.status_code == 401
        assert metric("traxjourney_logins_total",
                      provider="password", result="failure") - before == 1
        assert metric("traxjourney_logins_total",
                      provider="password", result="success") == ok_before

    def test_unknown_user_is_counted_as_a_failure(self, client, metric):
        """A brute-force sweep hits this path, not the wrong-password one —
        both have to land in the same counter for the rate to mean anything."""
        before = metric("traxjourney_logins_total", provider="password", result="failure")

        resp = client.post(
            "/api/auth/token", json={"username": "nobody@example.com", "password": "x"},
        )

        assert resp.status_code == 401
        assert metric("traxjourney_logins_total",
                      provider="password", result="failure") - before == 1


class TestGoogleAuthCounters:
    def _id_info(self, sub="google-sub-1"):
        return {"sub": sub, "email": "rider@example.com", "name": "Rider",
                "picture": "https://example.com/a.png"}

    def test_verification_failure_is_counted(self, client, metric):
        import api.auth as auth

        before = metric("traxjourney_logins_total", provider="google", result="failure")
        with patch.object(auth, "verify_oauth2_token", side_effect=ValueError("bad")), \
                patch.object(auth, "_google_client_id", "client-123"):
            resp = client.post("/api/auth/google", json={"id_token": "x"})

        assert resp.status_code == 401
        assert metric("traxjourney_logins_total",
                      provider="google", result="failure") - before == 1

    def test_first_google_login_counts_as_a_registration_too(self, client, metric):
        import api.auth as auth

        logins_before = metric("traxjourney_logins_total", provider="google", result="success")
        regs_before = metric("traxjourney_registrations_total", provider="google")

        with patch.object(auth, "verify_oauth2_token", return_value=self._id_info()), \
                patch.object(auth, "_google_client_id", "client-123"):
            resp = client.post("/api/auth/google", json={"id_token": "x"})

        assert resp.status_code == 200
        assert metric("traxjourney_logins_total",
                      provider="google", result="success") - logins_before == 1
        assert metric("traxjourney_registrations_total", provider="google") - regs_before == 1

    def test_returning_google_user_is_a_login_not_a_registration(self, client, metric):
        """Google sign-in has no separate sign-up call — the account is created
        on first sight — so the registration counter must fire exactly once per
        account, or new-user growth reads as total Google logins."""
        import api.auth as auth

        with patch.object(auth, "verify_oauth2_token", return_value=self._id_info()), \
                patch.object(auth, "_google_client_id", "client-123"):
            client.post("/api/auth/google", json={"id_token": "x"})

            logins_before = metric("traxjourney_logins_total",
                                   provider="google", result="success")
            regs_before = metric("traxjourney_registrations_total", provider="google")

            resp = client.post("/api/auth/google", json={"id_token": "x"})

        assert resp.status_code == 200
        assert metric("traxjourney_logins_total",
                      provider="google", result="success") - logins_before == 1
        assert metric("traxjourney_registrations_total", provider="google") == regs_before


class TestAppOpenedCounter:
    """traxjourney_app_opens_total (issue: Grafana login graph undercounted
    return visits that resumed a cached session instead of logging in)."""

    def test_resumed_session_is_counted(self, client, metric):
        before = metric("traxjourney_app_opens_total", session_state="resumed")

        resp = client.post("/api/auth/app-opened", json={"session_state": "resumed"})

        assert resp.status_code == 200
        assert metric("traxjourney_app_opens_total", session_state="resumed") - before == 1

    def test_login_required_is_counted_separately(self, client, metric):
        before = metric("traxjourney_app_opens_total", session_state="login_required")

        resp = client.post("/api/auth/app-opened", json={"session_state": "login_required"})

        assert resp.status_code == 200
        assert metric("traxjourney_app_opens_total",
                      session_state="login_required") - before == 1

    def test_unknown_session_state_is_rejected(self, client, metric):
        resp = client.post("/api/auth/app-opened", json={"session_state": "bogus"})

        assert resp.status_code == 422
