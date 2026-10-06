"""Tests for the zero-knowledge encryption endpoints (issue #26).

Covers:
  1. enable — stores device + recovery wraps, flips encryption_enabled, returns state
  2. enable — rejects a second enable (409) and an unknown recovery method (422)
  3. status — reports enabled + recovery methods; returns this device's wrapped CMK
  4. status — a different/unknown device public key is not "registered"
  5. isolation — one user's key material never leaks to another user
  6. recovery key confirm or replace (Decision 16) — an unconfirmed recovery key
     is confirmed only for the exact wrap shown, and replaced only while
     unconfirmed
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
import api.encryption as enc_module
from api.encryption import RecoveryKeyReplaceIn, replace_recovery_key
from api.encryption import router as encryption_router
from models.project_db import DBDeviceKey, DBRecoveryWrap
from models.user import UserInfo


def _make_app(user_payload: dict) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: user_payload
    app.include_router(encryption_router)
    return app


def _enable_body(method: str = "recovery_key") -> dict:
    return {
        "device": {
            "public_key": "DEVPUB_A",
            "label": "Chrome on Windows",
            "wrapped_cmk": "WRAP_DEV_A",
            "ephemeral_public_key": "EPH_A",
        },
        "recovery": {
            "method": method,
            "wrapped_cmk": "WRAP_REC",
            "salt": "SALT",
            "kdf_params_json": '{"memoryKib":19456}' if method == "qna" else None,
        },
    }


@pytest.fixture
def enc_env(monkeypatch):
    """In-memory DB + one user; yields (client, user_id, engine)."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        user_id = user.id

    client = TestClient(_make_app({"sub": str(user_id), "email": "alice@example.com"}))
    return client, user_id, test_engine


def test_enable_stores_wraps_and_flips_flag(enc_env):
    client, user_id, engine = enc_env
    resp = client.post("/api/encryption/enable", json=_enable_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["recovery_methods"] == ["recovery_key"]
    assert body["device"] == {
        "registered": True,
        "approved": True,
        "wrapped_cmk": "WRAP_DEV_A",
        "ephemeral_public_key": "EPH_A",
    }

    with Session(engine) as sess:
        ui = sess.get(UserInfo, user_id)
        assert ui.encryption_enabled is True
        devices = sess.exec(select(DBDeviceKey).where(DBDeviceKey.user_info_id == user_id)).all()
        assert len(devices) == 1 and devices[0].approved is True
        recs = sess.exec(select(DBRecoveryWrap).where(DBRecoveryWrap.user_info_id == user_id)).all()
        assert len(recs) == 1 and recs[0].method == "recovery_key"


def test_enable_twice_conflicts(enc_env):
    client, _, _ = enc_env
    assert client.post("/api/encryption/enable", json=_enable_body()).status_code == 201
    resp = client.post("/api/encryption/enable", json=_enable_body())
    assert resp.status_code == 409


def test_enable_rejects_unknown_recovery_method(enc_env):
    client, _, _ = enc_env
    resp = client.post("/api/encryption/enable", json=_enable_body(method="palm_print"))
    assert resp.status_code == 422


def test_status_returns_this_devices_wrapped_cmk(enc_env):
    client, _, _ = enc_env
    client.post("/api/encryption/enable", json=_enable_body(method="qna"))

    resp = client.get("/api/encryption/status", params={"device_public_key": "DEVPUB_A"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["recovery_methods"] == ["qna"]
    assert body["device"]["registered"] is True
    assert body["device"]["wrapped_cmk"] == "WRAP_DEV_A"


def test_status_unknown_device_not_registered(enc_env):
    client, _, _ = enc_env
    client.post("/api/encryption/enable", json=_enable_body())
    resp = client.get("/api/encryption/status", params={"device_public_key": "SOME_OTHER_DEVICE"})
    assert resp.status_code == 200
    assert resp.json()["device"] == {
        "registered": False, "approved": False,
        "wrapped_cmk": None, "ephemeral_public_key": None,
    }


def test_status_before_enable_is_disabled(enc_env):
    client, _, _ = enc_env
    resp = client.get("/api/encryption/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["recovery_methods"] == []
    assert body["device"]["registered"] is False


# ── Cross-device approval lifecycle ─────────────────────────────────────────────

def test_register_pending_then_approve(enc_env):
    client, _, _ = enc_env
    client.post("/api/encryption/enable", json=_enable_body())  # device A trusted

    # Device B registers, lands pending (not approved, no wrapped CMK).
    reg = client.post("/api/encryption/devices/register",
                      json={"public_key": "DEVPUB_B", "label": "Phone"})
    assert reg.status_code == 200
    assert reg.json() == {
        "registered": True, "approved": False,
        "wrapped_cmk": None, "ephemeral_public_key": None,
    }

    # It shows up in the pending list.
    pending = client.get("/api/encryption/devices/pending").json()
    assert [p["public_key"] for p in pending] == ["DEVPUB_B"]

    # A trusted device approves it by uploading a wrap.
    appr = client.post("/api/encryption/devices/approve", json={
        "public_key": "DEVPUB_B",
        "wrapped_cmk": "WRAP_DEV_B",
        "ephemeral_public_key": "EPH_B",
    })
    assert appr.status_code == 200
    assert appr.json()["approved"] is True

    # Pending list is now empty; device B's status returns its wrapped CMK.
    assert client.get("/api/encryption/devices/pending").json() == []
    st = client.get("/api/encryption/status",
                    params={"device_public_key": "DEVPUB_B"}).json()
    assert st["device"]["approved"] is True
    assert st["device"]["wrapped_cmk"] == "WRAP_DEV_B"


def test_register_requires_encryption_enabled(enc_env):
    client, _, _ = enc_env
    resp = client.post("/api/encryption/devices/register",
                       json={"public_key": "DEVPUB_B", "label": ""})
    assert resp.status_code == 409


def test_register_is_idempotent(enc_env):
    client, _, _ = enc_env
    client.post("/api/encryption/enable", json=_enable_body())
    a = client.post("/api/encryption/devices/register",
                    json={"public_key": "DEVPUB_B", "label": "Phone"})
    b = client.post("/api/encryption/devices/register",
                    json={"public_key": "DEVPUB_B", "label": "Phone again"})
    assert a.status_code == 200 and b.status_code == 200
    assert len(client.get("/api/encryption/devices/pending").json()) == 1


def test_approve_unknown_device_404(enc_env):
    client, _, _ = enc_env
    client.post("/api/encryption/enable", json=_enable_body())
    resp = client.post("/api/encryption/devices/approve", json={
        "public_key": "NOPE", "wrapped_cmk": "X", "ephemeral_public_key": "Y",
    })
    assert resp.status_code == 404


def test_recovery_wrap_fetch(enc_env):
    client, _, _ = enc_env
    client.post("/api/encryption/enable", json=_enable_body(method="qna"))

    ok = client.get("/api/encryption/recovery/qna")
    assert ok.status_code == 200
    body = ok.json()
    assert body["wrapped_cmk"] == "WRAP_REC"
    assert body["salt"] == "SALT"
    assert body["kdf_params_json"] == '{"memoryKib":19456}'

    # A method the user didn't configure → 404.
    assert client.get("/api/encryption/recovery/recovery_key").status_code == 404


# ── Recovery key confirm or replace (Decision 16) ──────────────────────────────

def _recovery_rows(engine, user_id):
    with Session(engine) as sess:
        return sess.exec(
            select(DBRecoveryWrap).where(DBRecoveryWrap.user_info_id == user_id)
        ).all()


def _unconfirmed(client):
    return client.get("/api/encryption/status").json()["unconfirmed_recovery_methods"]


def _confirm(client, wrap, method="recovery_key"):
    return client.post("/api/encryption/recovery/confirm",
                       json={"method": method, "wrapped_cmk": wrap})


def _replace(client, wrap, salt="SALT_NEW"):
    return client.put("/api/encryption/recovery/recovery_key",
                      json={"wrapped_cmk": wrap, "salt": salt})


def test_enable_with_recovery_key_stores_it_unconfirmed(enc_env):
    client, user_id, engine = enc_env
    resp = client.post("/api/encryption/enable", json=_enable_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["unconfirmed_recovery_methods"] == ["recovery_key"]
    # The enable response carries the stored wrap, for the confirm call.
    assert body["recovery_wrapped_cmk"] == "WRAP_REC"

    [row] = _recovery_rows(engine, user_id)
    assert row.confirmed is False
    assert _unconfirmed(client) == ["recovery_key"]


@pytest.mark.parametrize("method", ["passphrase", "qna"])
def test_enable_with_typed_method_stores_it_confirmed(enc_env, method):
    client, user_id, engine = enc_env
    resp = client.post("/api/encryption/enable", json=_enable_body(method=method))
    assert resp.status_code == 201, resp.text
    assert resp.json()["unconfirmed_recovery_methods"] == []

    [row] = _recovery_rows(engine, user_id)
    assert row.confirmed is True
    assert _unconfirmed(client) == []


def test_confirm_the_stored_wrap_marks_it_and_is_idempotent(enc_env):
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body())

    assert _confirm(client, "WRAP_REC").status_code == 204
    assert _unconfirmed(client) == []
    assert _recovery_rows(engine, user_id)[0].confirmed is True

    # A retried confirm of the same wrap is a success, not a conflict.
    assert _confirm(client, "WRAP_REC").status_code == 204
    assert _unconfirmed(client) == []


def test_confirm_another_wrap_conflicts_and_confirms_nothing(enc_env):
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body())

    assert _confirm(client, "NOT_THE_WRAP_SHOWN").status_code == 409
    [row] = _recovery_rows(engine, user_id)
    assert row.confirmed is False
    assert _unconfirmed(client) == ["recovery_key"]


def test_confirm_a_method_without_a_wrap_is_404(enc_env):
    client, _, _ = enc_env
    client.post("/api/encryption/enable", json=_enable_body())
    assert _confirm(client, "WRAP_REC", method="passphrase").status_code == 404


def test_confirm_and_replace_require_encryption_enabled(enc_env):
    client, _, _ = enc_env
    assert _confirm(client, "WRAP_REC").status_code == 409
    assert _replace(client, "WRAP_NEW").status_code == 409


def test_replace_body_shape_is_validated(enc_env):
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body())
    resp = client.put("/api/encryption/recovery/recovery_key",
                      json={"wrapped_cmk": "WRAP_NEW"})
    assert resp.status_code == 422
    assert _recovery_rows(engine, user_id)[0].wrapped_cmk == "WRAP_REC"


def test_replace_while_unconfirmed_updates_the_one_row(enc_env):
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body())

    resp = _replace(client, "WRAP_NEW", salt="SALT_NEW")
    assert resp.status_code == 200, resp.text
    assert resp.json()["method"] == "recovery_key"
    assert resp.json()["wrapped_cmk"] == "WRAP_NEW"
    assert resp.json()["salt"] == "SALT_NEW"

    [row] = _recovery_rows(engine, user_id)
    assert (row.method, row.wrapped_cmk, row.salt) == ("recovery_key", "WRAP_NEW", "SALT_NEW")
    assert row.confirmed is False
    assert row.version == 1
    assert _unconfirmed(client) == ["recovery_key"]


def test_replace_after_confirm_conflicts_and_keeps_the_key(enc_env):
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body())
    assert _confirm(client, "WRAP_REC").status_code == 204

    assert _replace(client, "WRAP_STOLEN").status_code == 409
    [row] = _recovery_rows(engine, user_id)
    assert (row.wrapped_cmk, row.salt, row.confirmed) == ("WRAP_REC", "SALT", True)


def test_replace_without_a_recovery_key_conflicts(enc_env):
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body(method="passphrase"))

    assert _replace(client, "WRAP_NEW").status_code == 409
    [row] = _recovery_rows(engine, user_id)
    assert (row.method, row.wrapped_cmk) == ("passphrase", "WRAP_REC")


def test_two_replaces_then_confirm_of_the_first_conflicts(enc_env):
    """U5b-R1-1: device A shows K1, device B then replaces with K2; A's Done
    must not confirm K2, which the key A's user saved cannot unwrap."""
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body())

    assert _replace(client, "WRAP_K1").status_code == 200
    assert _replace(client, "WRAP_K2").status_code == 200

    assert _confirm(client, "WRAP_K1").status_code == 409
    [row] = _recovery_rows(engine, user_id)
    assert (row.wrapped_cmk, row.confirmed) == ("WRAP_K2", False)

    assert _confirm(client, "WRAP_K2").status_code == 204


def test_replace_returns_its_own_wrap_when_another_commits_before_it_responds(
        enc_env, monkeypatch):
    """U5b-R2-1: a second replace commits between the first's write and its
    response. The first must answer with the wrap it wrote, so its client
    confirms the key it shows (and gets 409), not the other device's."""
    client, user_id, engine = enc_env
    client.post("/api/encryption/enable", json=_enable_body())

    real_get_session = enc_module.get_session
    fired = []

    @contextmanager
    def racing_session():
        with real_get_session() as sess:
            real_commit = sess.commit

            def commit():
                real_commit()
                if not fired:
                    fired.append(True)
                    monkeypatch.setattr(enc_module, "get_session", real_get_session)
                    replace_recovery_key(
                        RecoveryKeyReplaceIn(wrapped_cmk="WRAP_B", salt="SALT_B"),
                        {"sub": str(user_id)})

            sess.commit = commit
            yield sess

    monkeypatch.setattr(enc_module, "get_session", racing_session)
    resp = _replace(client, "WRAP_A", salt="SALT_A")
    assert fired, "the interleaved replace never ran"
    assert resp.status_code == 200, resp.text
    assert resp.json()["wrapped_cmk"] == "WRAP_A"
    assert resp.json()["salt"] == "SALT_A"

    [row] = _recovery_rows(engine, user_id)
    assert row.wrapped_cmk == "WRAP_B"
    assert _confirm(client, "WRAP_A").status_code == 409


def test_another_user_cannot_confirm_or_replace_the_wrap(enc_env):
    client_a, user_a, engine = enc_env
    client_a.post("/api/encryption/enable", json=_enable_body())

    with Session(engine) as sess:
        bob = UserInfo(display_name="Bob", email="bob@example.com")
        sess.add(bob)
        sess.commit()
        sess.refresh(bob)
        user_b = bob.id
    client_b = TestClient(_make_app({"sub": str(user_b), "email": "bob@example.com"}))
    body_b = _enable_body()
    body_b["device"]["public_key"] = "DEVPUB_BOB"
    body_b["recovery"]["wrapped_cmk"] = "WRAP_REC_BOB"
    assert client_b.post("/api/encryption/enable", json=body_b).status_code == 201

    # Bob sending Alice's wrap matches nothing of his: conflict, nothing confirmed.
    assert _confirm(client_b, "WRAP_REC").status_code == 409
    # Bob's replace touches only his own row.
    assert _replace(client_b, "WRAP_BOB_NEW").status_code == 200

    [row_a] = _recovery_rows(engine, user_a)
    assert (row_a.wrapped_cmk, row_a.salt, row_a.confirmed) == ("WRAP_REC", "SALT", False)
    [row_b] = _recovery_rows(engine, user_b)
    assert (row_b.wrapped_cmk, row_b.confirmed) == ("WRAP_BOB_NEW", False)
    assert _unconfirmed(client_a) == ["recovery_key"]
