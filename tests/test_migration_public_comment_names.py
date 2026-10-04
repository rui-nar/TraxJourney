"""Data-migration test for 87200bcb9342 — stored comment and like author names
no longer show a sign-in address (issue #507).

Builds a throwaway SQLite DB at a revision BEFORE the migration's own parent
(so the intermediate migration runs in the same pass, as on an install that
skipped a release), seeds comments and likes whose stored author name is the
author's email, linked username, blank or a real name, upgrades to head, and
asserts which ones now read "Traveller". Hermetic — never touches the
developer's real db.
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, MetaData, Table, text

from models.project_db import DBMemory, DBMemoryComment, DBMemoryLike, DBProject
from models.user import LocalUser, UserInfo

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OLD_REV = "b7e4d2c9a1f0"       # before the migration's down_revision
_PREV_REV = "e3a91c5d7f20"      # down_revision of the migration


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _seed_row(engine, table_name: str, obj) -> None:
    """Insert *obj* using only the columns the table has at the seeded revision."""
    data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_sa_")}
    tbl = Table(table_name, MetaData(), autoload_with=engine)
    data = {k: v for k, v in data.items() if k in tbl.columns}
    with engine.begin() as conn:
        conn.execute(insert(tbl), data)


# (id, author user_info_id, stored name, expected name after the upgrade)
_COMMENTS = [
    (1, 1, "ana.silva@example.COM ", "Traveller"),   # author's email, other case
    (2, 2, "Bob@Example.org", "Traveller"),          # linked username, empty email
    (3, 3, "   ", "Traveller"),                      # blank
    (4, 3, "Cleo", "Cleo"),                          # a real name
    (5, 3, "Ana.Silva@Example.com", "Ana.Silva@Example.com"),  # someone else's
    (6, 99, "dan@example.com", "dan@example.com"),   # author gone
    (7, 99, "", "Traveller"),                        # author gone, blank
    (8, 4, "ÉLODIE@example.fr", "Traveller"),        # non-ASCII case fold
]
_LIKES = [
    (1, 1, "ANA.SILVA@example.com", "Traveller"),
    (2, 2, " bob@example.org", "Traveller"),
    (3, 3, "", "Traveller"),
    (4, 3, "Cleo", "Cleo"),
    (5, 99, "dan@example.com", "dan@example.com"),
]


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db_path = tmp_path / "public_names_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    cfg = _cfg(db_path)
    command.upgrade(cfg, _OLD_REV)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    _seed_row(engine, "localuser", LocalUser(id=1, username="bob@example.org"))
    _seed_row(engine, "userinfo", UserInfo(
        id=1, email="Ana.Silva@Example.com", display_name="Ana.Silva@Example.com"))
    _seed_row(engine, "userinfo", UserInfo(
        id=2, email="", local_auth_id=1, display_name="bob@example.org"))
    _seed_row(engine, "userinfo", UserInfo(
        id=3, email="cleo@example.net", display_name="Cleo"))
    _seed_row(engine, "userinfo", UserInfo(
        id=4, email="élodie@example.fr", display_name="élodie@example.fr"))
    _seed_row(engine, "project", DBProject(id=1, user_info_id=1, name="Trip"))
    _seed_row(engine, "memory", DBMemory(id=1, project_id=1, date="2026-01-01"))
    for cid, uid, name, _ in _COMMENTS:
        _seed_row(engine, "memory_comment", DBMemoryComment(
            id=cid, memory_id=1, user_info_id=uid, commenter_name=name, text="hi"))
    for lid, uid, name, _ in _LIKES:
        _seed_row(engine, "memory_like", DBMemoryLike(
            id=lid, memory_id=1, user_info_id=uid, liker_name=name))
    return cfg, engine


def _names(engine) -> tuple[dict, dict]:
    with engine.connect() as conn:
        comments = {r[0]: r[1] for r in conn.execute(
            text("SELECT id, commenter_name FROM memory_comment"))}
        likes = {r[0]: r[1] for r in conn.execute(
            text("SELECT id, liker_name FROM memory_like"))}
    return comments, likes


def _expected() -> tuple[dict, dict]:
    return ({cid: want for cid, _, _, want in _COMMENTS},
            {lid: want for lid, _, _, want in _LIKES})


def test_upgrade_replaces_addresses_and_blanks_with_traveller(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, "head")
    comments, likes = _names(engine)
    want_comments, want_likes = _expected()
    assert comments == want_comments
    assert likes == want_likes


def test_upgrade_is_idempotent(seeded):
    cfg, engine = seeded
    command.upgrade(cfg, "head")
    first = _names(engine)
    command.downgrade(cfg, _PREV_REV)
    assert _names(engine) == first, "downgrade cannot restore the addresses"
    command.upgrade(cfg, "head")
    assert _names(engine) == first == _expected()


def test_upgrade_leaves_lock_version_alone(seeded):
    """Names are not in any payload a client caches against lock_version (the
    project payload carries counts only), so no trip needs refetching."""
    cfg, engine = seeded
    with engine.connect() as conn:
        before = conn.execute(text("SELECT lock_version FROM project")).scalar()
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT lock_version FROM project")).scalar() == before
