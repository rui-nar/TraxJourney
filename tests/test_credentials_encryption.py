"""Third-party credentials are stored encrypted, under a key held outside the
database (src/auth/credentials_crypto.py).

Covers the four columns (Strava access and refresh tokens, Polarsteps
remember_token, Immich API key): what reaches the table, the round trip, the
column binding, key rotation and its script, the startup check, the data
migration that encrypts existing rows, and that a Strava token refresh writes
nothing outside the database.

``conftest.py`` sets a key for the suite; the cases that need others set them
explicitly.
"""
from __future__ import annotations

import builtins
import logging
import pathlib
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

import models.db as db_module
import scripts.rotate_credentials_key as rotation
from models.user import ImmichToken, PolarstepsToken, StravaToken, UserInfo
from src.auth import credentials_crypto as crypto
from src.auth.credentials_crypto import (
    PREFIX,
    CredentialDecryptError,
    credentials_keyring,
    decrypt_credential,
    encrypt_credential,
    key_id_of,
)

KEY_A = "11" * 32
KEY_B = "22" * 32

# (model, attribute, table, column)
COLUMNS = [
    (StravaToken, "access_token", "stravatoken", "access_token"),
    (StravaToken, "refresh_token", "stravatoken", "refresh_token"),
    (PolarstepsToken, "remember_token", "polarstepstoken", "remember_token"),
    (ImmichToken, "api_key", "immichtoken", "api_key"),
]

SECRETS = {
    "access_token": "strava-access-1234",
    "refresh_token": "strava-refresh-5678",
    "remember_token": "123|polarsteps-cookie",
    "api_key": "immich-api-key-abcd",
}


def _use_keys(monkeypatch, current, retired=None):
    if current is None:
        monkeypatch.delenv(crypto.KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(crypto.KEY_ENV, current)
    if retired is None:
        monkeypatch.delenv(crypto.RETIRED_KEYS_ENV, raising=False)
    else:
        monkeypatch.setenv(crypto.RETIRED_KEYS_ENV, retired)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{(tmp_path / 'creds.db').as_posix()}")
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _seed(engine) -> int:
    """One user connected to all three services. Returns the user id."""
    with Session(engine) as sess:
        user = UserInfo(email="a@x.io", display_name="A")
        sess.add(user)
        sess.commit()
        uid = user.id
        sess.add(StravaToken(user_info_id=uid, access_token=SECRETS["access_token"],
                             refresh_token=SECRETS["refresh_token"], expires_at=1.0))
        sess.add(PolarstepsToken(user_info_id=uid, remember_token=SECRETS["remember_token"]))
        sess.add(ImmichToken(user_info_id=uid, server_url="https://immich.example",
                             api_key=SECRETS["api_key"]))
        sess.commit()
    return uid


def _raw(engine, table, column) -> str:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT {column} FROM {table}")).scalar_one()


def _orm(engine, model, attr) -> str:
    with Session(engine) as sess:
        return getattr(sess.exec(select(model)).one(), attr)


# ── What is stored ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,attr,table,column", COLUMNS)
def test_the_table_holds_ciphertext_naming_its_key(engine, model, attr, table, column):
    _seed(engine)
    stored = _raw(engine, table, column)
    assert SECRETS[attr] not in stored
    assert stored.startswith(PREFIX)
    assert key_id_of(stored) == credentials_keyring().current_id


@pytest.mark.parametrize("model,attr,table,column", COLUMNS)
def test_round_trip(engine, model, attr, table, column):
    _seed(engine)
    assert _orm(engine, model, attr) == SECRETS[attr]


def test_two_encryptions_of_one_value_differ():
    """A fresh nonce per value: equal credentials are not visibly equal."""
    a = encrypt_credential("same", "stravatoken.access_token")
    b = encrypt_credential("same", "stravatoken.access_token")
    assert a != b


def test_empty_stays_empty(engine):
    """'' is the "not connected" default; it carries no secret."""
    with Session(engine) as sess:
        sess.add(ImmichToken(user_info_id=1, server_url="https://i", api_key=""))
        sess.commit()
    assert _raw(engine, "immichtoken", "api_key") == ""
    assert _orm(engine, ImmichToken, "api_key") == ""


def test_an_update_statement_stores_ciphertext(engine):
    """api/strava.py persists a rotated token with a Core UPDATE, not the ORM."""
    from sqlalchemy import update
    _seed(engine)
    with Session(engine) as sess:
        sess.execute(update(StravaToken).values(refresh_token="rotated-refresh"))
        sess.commit()
    assert "rotated-refresh" not in _raw(engine, "stravatoken", "refresh_token")
    assert _orm(engine, StravaToken, "refresh_token") == "rotated-refresh"


# ── Column binding ──────────────────────────────────────────────────────────

def test_a_value_encrypted_for_one_column_does_not_decrypt_in_another():
    stored = encrypt_credential("secret", "stravatoken.access_token")
    assert decrypt_credential(stored, "stravatoken.access_token") == "secret"
    for other in ("stravatoken.refresh_token", "polarstepstoken.remember_token",
                  "immichtoken.api_key"):
        with pytest.raises(CredentialDecryptError):
            decrypt_credential(stored, other)


def test_a_value_moved_to_another_column_reads_as_empty(engine, caplog):
    _seed(engine)
    access = _raw(engine, "stravatoken", "access_token")
    with engine.begin() as conn:
        conn.execute(text("UPDATE immichtoken SET api_key = :v"), {"v": access})
    with caplog.at_level(logging.WARNING, logger="src.auth.credentials_crypto"):
        assert _orm(engine, ImmichToken, "api_key") == ""
    assert "immichtoken.api_key" in caplog.text
    assert SECRETS["access_token"] not in caplog.text


def test_a_tampered_value_does_not_decrypt():
    stored = encrypt_credential("secret", "immichtoken.api_key")
    flipped = stored[:-2] + ("A" if stored[-2] != "A" else "B") + stored[-1]
    with pytest.raises(CredentialDecryptError):
        decrypt_credential(flipped, "immichtoken.api_key")


def test_plaintext_left_in_the_table_is_not_returned(engine):
    """Nothing reads a plain-text credential back as if it were valid."""
    _seed(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE immichtoken SET api_key = 'plain-key'"))
    assert _orm(engine, ImmichToken, "api_key") == ""


# ── Key rotation ────────────────────────────────────────────────────────────

def test_a_retired_key_still_decrypts_and_writes_use_the_current_key(engine, monkeypatch):
    _use_keys(monkeypatch, KEY_A)
    _seed(engine)
    id_a = credentials_keyring().current_id

    _use_keys(monkeypatch, KEY_B, retired=KEY_A)
    id_b = credentials_keyring().current_id
    assert id_a != id_b
    assert _orm(engine, ImmichToken, "api_key") == SECRETS["api_key"]

    with Session(engine) as sess:
        tok = sess.exec(select(ImmichToken)).one()
        tok.api_key = "new-immich-key"
        sess.add(tok)
        sess.commit()
    assert key_id_of(_raw(engine, "immichtoken", "api_key")) == id_b
    assert key_id_of(_raw(engine, "polarstepstoken", "remember_token")) == id_a


def test_a_value_under_an_unlisted_key_reads_as_empty(engine, monkeypatch):
    """A lost key loses the credential, not the row: the service shows as
    disconnected and the user can reconnect it."""
    _use_keys(monkeypatch, KEY_A)
    _seed(engine)
    _use_keys(monkeypatch, KEY_B)
    assert _orm(engine, StravaToken, "access_token") == ""


def test_the_rotation_script_covers_every_encrypted_column():
    assert rotation.encrypted_columns() == sorted((t, c) for _m, _a, t, c in COLUMNS)


def test_the_rotation_script_reencrypts_under_the_current_key(engine, monkeypatch, capsys):
    _use_keys(monkeypatch, KEY_A)
    _seed(engine)
    _use_keys(monkeypatch, KEY_B, retired=KEY_A)
    id_b = credentials_keyring().current_id
    before = {c: _raw(engine, t, c) for _m, _a, t, c in COLUMNS}

    assert rotation.main([], engine=engine) == 0  # dry run
    assert {c: _raw(engine, t, c) for _m, _a, t, c in COLUMNS} == before
    out = capsys.readouterr().out
    assert "To re-encrypt: 4" in out and "Dry run" in out

    assert rotation.main(["--apply"], engine=engine) == 0
    for _m, _a, table, column in COLUMNS:
        assert key_id_of(_raw(engine, table, column)) == id_b
    out = capsys.readouterr().out
    assert all(secret not in out for secret in SECRETS.values())

    # The retired key can now go: everything reads under the new key alone.
    _use_keys(monkeypatch, KEY_B)
    for model, attr, _t, _c in COLUMNS:
        assert _orm(engine, model, attr) == SECRETS[attr]
    assert rotation.main([], engine=engine) == 0
    assert "Already under the current key: 4" in capsys.readouterr().out


def test_the_rotation_script_reports_values_no_key_decrypts(engine, monkeypatch, capsys):
    _use_keys(monkeypatch, KEY_A)
    _seed(engine)
    _use_keys(monkeypatch, KEY_B)
    assert rotation.main(["--apply"], engine=engine) == 1
    assert "Unreadable with the configured keys: immichtoken.api_key row 1" in capsys.readouterr().out
    _use_keys(monkeypatch, KEY_A)
    assert _orm(engine, ImmichToken, "api_key") == SECRETS["api_key"]  # left as it was


# ── Startup check ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("current,retired,says", [
    (None, None, "is not configured"),
    ("   ", None, "is not configured"),
    ("not-hex-" * 8, None, "not a hexadecimal"),
    ("ab" * 16, None, "must be 32 bytes"),
    (KEY_A, "zz", "not a hexadecimal"),
    (KEY_A, "ab" * 8, "must be 32 bytes"),
], ids=["unset", "blank", "not-hex", "short", "retired-not-hex", "retired-short"])
def test_an_unusable_key_is_refused_naming_the_fix(monkeypatch, current, retired, says):
    _use_keys(monkeypatch, current, retired)
    with pytest.raises(RuntimeError) as exc:
        credentials_keyring()
    assert says in str(exc.value)
    assert "openssl rand -hex 32" in str(exc.value)


@pytest.mark.parametrize("current", [None, "ab" * 16], ids=["unset", "short"])
def test_startup_refuses_without_a_usable_key(monkeypatch, current):
    import api.router as router
    from fastapi.testclient import TestClient

    _use_keys(monkeypatch, current)
    # The worker path: the lifespan checks the keys and then yields.
    monkeypatch.setattr(router, "_IS_API_PROCESS", False)
    with pytest.raises(RuntimeError, match="CREDENTIALS_ENCRYPTION_KEY"):
        with TestClient(router.app):
            pass


def test_startup_succeeds_with_a_usable_key(monkeypatch):
    import api.router as router
    from fastapi.testclient import TestClient

    _use_keys(monkeypatch, KEY_A, retired=KEY_B)
    monkeypatch.setattr(router, "_IS_API_PROCESS", False)
    with TestClient(router.app) as client:
        assert client.get("/api/version").status_code == 200


# ── Data migration e3a91c5d7f20 ─────────────────────────────────────────────

_PREV_REV = "b7e4d2c9a1f0"  # down_revision of e3a91c5d7f20


def _alembic(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, url


def test_migration_encrypts_existing_values_and_downgrade_restores_them(tmp_path, monkeypatch):
    cfg, url = _alembic(tmp_path, monkeypatch)
    command.upgrade(cfg, _PREV_REV)

    already = encrypt_credential("already-encrypted", "immichtoken.api_key")
    eng = create_engine(url)
    with eng.begin() as conn:
        for uid in (1, 2):
            conn.execute(text(
                "INSERT INTO userinfo (id, google_sub, display_name, email, avatar_url, "
                "auth_provider, is_admin, email_verified, created_at, encryption_enabled) "
                "VALUES (:u, '', 'a', :e, '', 'local', 0, 0, 0, 0)"), {"u": uid, "e": f"{uid}@x.io"})
        conn.execute(text(
            "INSERT INTO stravatoken (user_info_id, access_token, refresh_token, expires_at) "
            "VALUES (1, :a, :r, 0)"), {"a": SECRETS["access_token"], "r": SECRETS["refresh_token"]})
        conn.execute(text(
            "INSERT INTO polarstepstoken (user_info_id, remember_token, polarsteps_user_id, "
            "polarsteps_username) VALUES (1, :t, 123, 'p')"), {"t": SECRETS["remember_token"]})
        conn.execute(text(
            "INSERT INTO immichtoken (user_info_id, server_url, api_key) VALUES (1, 'https://i', :k)"),
            {"k": SECRETS["api_key"]})
        # An empty value and one already encrypted are left as they are.
        conn.execute(text(
            "INSERT INTO immichtoken (user_info_id, server_url, api_key) VALUES (2, 'https://j', :k)"),
            {"k": already})
        conn.execute(text(
            "INSERT INTO polarstepstoken (user_info_id, remember_token, polarsteps_user_id, "
            "polarsteps_username) VALUES (2, '', 0, '')"))

    command.upgrade(cfg, "head")

    with eng.connect() as conn:
        for _m, attr, table, column in COLUMNS:
            stored = conn.execute(text(
                f"SELECT {column} FROM {table} WHERE user_info_id = 1")).scalar_one()
            assert stored.startswith(PREFIX)
            assert decrypt_credential(stored, f"{table}.{column}") == SECRETS[attr]
        assert conn.execute(text(
            "SELECT api_key FROM immichtoken WHERE user_info_id = 2")).scalar_one() == already
        assert conn.execute(text(
            "SELECT remember_token FROM polarstepstoken WHERE user_info_id = 2")).scalar_one() == ""
    with Session(eng) as sess:
        tok = sess.exec(select(StravaToken)).one()
        assert (tok.access_token, tok.refresh_token) == (SECRETS["access_token"], SECRETS["refresh_token"])

    command.downgrade(cfg, _PREV_REV)

    with eng.connect() as conn:
        for _m, attr, table, column in COLUMNS:
            assert conn.execute(text(
                f"SELECT {column} FROM {table} WHERE user_info_id = 1")).scalar_one() == SECRETS[attr]
        assert conn.execute(text(
            "SELECT api_key FROM immichtoken WHERE user_info_id = 2")).scalar_one() == "already-encrypted"
    eng.dispose()


def test_migration_needs_no_key_when_there_is_nothing_to_encrypt(tmp_path, monkeypatch):
    """CI runs ``alembic upgrade head && alembic check`` on an empty database
    with no key configured."""
    cfg, _url = _alembic(tmp_path, monkeypatch)
    _use_keys(monkeypatch, None)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PREV_REV)


# ── No credential file ──────────────────────────────────────────────────────

def test_a_strava_token_refresh_is_stored_only_in_the_database(engine, monkeypatch):
    """The client used to mirror refreshed tokens into ~/.config/traxjourney;
    now the only copy is the encrypted row."""
    import api.strava as strava_module
    from src.auth.oauth import OAuth2Session
    from src.config.settings import Config as AppConfig

    cfg = AppConfig()
    cfg.set("strava.client_id", "id")
    cfg.set("strava.client_secret", "secret")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(strava_module, "_cfg", cfg)
    _seed(engine)
    with Session(engine) as sess:
        row = sess.exec(select(StravaToken)).one()
        client = strava_module._strava_client_for_token(row)
    assert client.token_data["expires_at"] < time.time()  # seeded expired

    refreshed = {"access_token": "fresh-access", "refresh_token": "fresh-refresh",
                 "expires_at": time.time() + 3600}
    monkeypatch.setattr(OAuth2Session, "refresh_token", lambda self, rt: refreshed)

    # Record — and refuse — any file write while the refresh runs. The database
    # is already open, so its own writes do not come through these.
    writes = []
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **kw):
        if any(m in mode for m in "wax+"):
            writes.append(str(file))
            raise PermissionError(f"unexpected write to {file}")
        return real_open(file, mode, *a, **kw)

    def guarded(name):
        def refuse(self, *a, **kw):
            writes.append(f"{name}:{self}")
            raise PermissionError(f"unexpected {name} of {self}")
        return refuse

    monkeypatch.setattr(builtins, "open", guarded_open)
    for name in ("write_text", "write_bytes", "mkdir", "touch"):
        monkeypatch.setattr(pathlib.Path, name, guarded(name))

    client._ensure_token()

    monkeypatch.undo()  # restore file access before reading the database back
    assert writes == []
    assert client.token_data["access_token"] == "fresh-access"
    assert _orm(engine, StravaToken, "refresh_token") == "fresh-refresh"
    assert "fresh-refresh" not in _raw(engine, "stravatoken", "refresh_token")
