"""Account deletion removes every row the account owns, in every table.

``test_account_deletion.py`` seeds a hand-picked list of tables, which is how
the Immich connection and the poster, video and route jobs were left behind:
nobody added them to the list. The guard here works from the schema instead.
Every column that points at ``userinfo.id`` (or is named like an owner
column) gets a row holding the deleted account's id, and after
``delete_user_and_data`` no such row may be left — unless the column is in
``_KEPT``, with the reason it is kept. A table added later is covered without
touching this file.

Also covers migration b7e4d2c9a1f0, which removes the rows earlier deletions
left behind.
"""
from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.main import default_registry

import models.billing  # noqa: F401 — registers every table on the metadata
import models.db as db_module
import models.project_db  # noqa: F401
import src.admin.storage as storage_mod
import src.auth.account_deletion as deletion_mod
from models.project_db import DBPosterJob, DBProject, DBRouteJob, DBVideoJob
from models.user import ImmichToken, LocalUser, UserInfo
from src.auth.account_deletion import delete_user_and_data

#: Owner-like column names, caught even without a foreign key declared.
_OWNER_NAMES = {"user_info_id", "owner_id", "user_id", "owner_user_id"}

#: Columns pointing at an account whose rows deletion deliberately keeps.
_KEPT = {
    # Who sent the invitation that made someone a member. The membership is
    # the member's (in the project owner's trip), not the inviter's: an
    # editor who invited a friend and then deleted their account must not
    # remove that friend from the trip.
    ("projectmember", "invited_by"),
}


def _owner_columns(table) -> list[str]:
    return [
        c.name for c in table.columns
        if c.name in _OWNER_NAMES
        or any(fk.column.table.name == "userinfo" for fk in c.foreign_keys)
    ]


def _owned_models() -> list[tuple[type, list[str]]]:
    out = []
    for mapper in default_registry.mappers:
        table = mapper.local_table
        if table.name == "userinfo":
            continue
        cols = _owner_columns(table)
        if cols:
            out.append((mapper.class_, cols))
    return sorted(out, key=lambda m: m[0].__tablename__)


_counter = itertools.count(1_000_000)


def _row(cls, owners: dict[str, int]):
    """An instance of *cls* with *owners* set and every required field filled.

    Model defaults are kept (a subscription stays ``none``, with no customer,
    so deletion has nothing to cancel). Fields without one get a value no real
    row uses: a large int, so a ``project_id`` never lands on one of the
    seeded projects by accident.
    """
    table = cls.__table__
    values = dict(owners)
    for name, field in cls.model_fields.items():
        if name in values or not field.is_required():
            continue
        col = table.columns[name]
        if col.primary_key and col.autoincrement is True:
            continue
        kind = field.annotation
        n = next(_counter)
        values[name] = {int: n, float: 0.0, bool: False, str: f"seed-{n}",
                        bytes: b""}[kind]
    return cls(**values)


@pytest.fixture
def engine(monkeypatch, tmp_path):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    # The seeded Strava token is placeholder text: never send it anywhere.
    monkeypatch.setattr(deletion_mod, "deauthorize_strava", lambda *a, **k: False)
    SQLModel.metadata.create_all(test_engine)
    return test_engine


def _mk_user(sess, email) -> int:
    local = LocalUser(username=email, password_hash=b"x")
    sess.add(local)
    sess.commit()
    ui = UserInfo(local_auth_id=local.id, display_name=email, email=email)
    sess.add(ui)
    sess.commit()
    return ui.id


def test_the_scan_finds_the_tables_it_guards():
    """The guard is only as good as the scan: it must see the known owners."""
    names = {cls.__tablename__ for cls, _ in _owned_models()}
    assert {"immichtoken", "posterjob", "videojob", "routejob", "stravatoken",
            "project", "subscription"} <= names
    for table, column in _KEPT:
        assert column in _owner_columns(SQLModel.metadata.tables[table]), (
            f"_KEPT names {table}.{column}, which is no longer an owner column")


def test_deletion_clears_every_column_that_points_at_the_account(engine):
    models = _owned_models()
    with Session(engine) as sess:
        uid = _mk_user(sess, "gone@x.io")
        oid = _mk_user(sess, "stays@x.io")
        for cls, cols in models:
            # One row per owner column holding the deleted account, the other
            # owner columns holding the other account…
            for col in cols:
                sess.add(_row(cls, {c: (uid if c == col else oid) for c in cols}))
            # …and the other account's own row, which must survive.
            sess.add(_row(cls, {c: oid for c in cols}))
            sess.commit()

    with Session(engine) as sess:
        delete_user_and_data(sess, uid)

    left, lost = [], []
    with Session(engine) as sess:
        for cls, cols in models:
            table = cls.__table__
            for col in cols:
                if (table.name, col) in _KEPT:
                    continue
                n = sess.execute(select(func.count()).select_from(table)
                                 .where(table.c[col] == uid)).scalar_one()
                if n:
                    left.append(f"{table.name}.{col}")
            kept = sess.execute(
                select(func.count()).select_from(table)
                .where(*(table.c[c] == oid for c in cols))
            ).scalar_one()
            if not kept:
                lost.append(table.name)
    assert left == [], (
        f"deleting the account left rows pointing at it in {left}: delete them "
        "in delete_user_and_data, or add the column to _KEPT with the reason")
    assert lost == [], f"deleting one account removed another's rows in {lost}"


def test_jobs_another_user_started_on_the_deleted_trip_stay(engine):
    """A companion's poster, video and route jobs on the owner's trip are the
    companion's rows (their files sit in the companion's folder): deleting the
    owner removes the owner's jobs and Immich connection, not those."""
    with Session(engine) as sess:
        owner = _mk_user(sess, "owner@x.io")
        companion = _mk_user(sess, "comp@x.io")
        proj = DBProject(user_info_id=owner, name="Trip")
        sess.add(proj)
        sess.commit()
        pid = proj.id
        sess.add(ImmichToken(user_info_id=owner, server_url="https://i", api_key="k"))
        for uid_ in (owner, companion):
            sess.add(DBPosterJob(project_id=pid, user_info_id=uid_))
            sess.add(DBVideoJob(project_id=pid, user_info_id=uid_))
            sess.add(DBRouteJob(project_id=pid, user_info_id=uid_,
                                project_name="Trip", segment_id="s"))
        sess.commit()

    with Session(engine) as sess:
        delete_user_and_data(sess, owner)

    with Session(engine) as sess:
        assert sess.exec(select(ImmichToken)).all() == []
        for model in (DBPosterJob, DBVideoJob, DBRouteJob):
            assert [j.user_info_id for j in sess.exec(select(model)).all()] == [companion]


# ── Data migration b7e4d2c9a1f0: rows earlier deletions left behind ──────────

_PREV_REV = "c519a0b1d2e3"  # down_revision of b7e4d2c9a1f0


def test_migration_deletes_rows_of_accounts_already_deleted(tmp_path, monkeypatch):
    """From the previous revision to head on a database holding rows of an
    account deleted before deletion removed them: those go, the rows of the
    account that still exists stay."""
    db_path = tmp_path / "orphans.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, _PREV_REV)

    eng = create_engine(url)
    with eng.begin() as conn:
        # Account 1 exists; account 2 was deleted earlier.
        conn.execute(text(
            "INSERT INTO userinfo (id, google_sub, display_name, email, avatar_url, "
            "auth_provider, is_admin, email_verified, created_at, encryption_enabled) "
            "VALUES (1, '', 'a', 'a@x.io', '', 'local', 0, 0, 0, 0)"))
        for uid in (1, 2):
            conn.execute(text(
                "INSERT INTO immichtoken (user_info_id, server_url, api_key) "
                "VALUES (:u, 'https://i', 'k')"), {"u": uid})
            conn.execute(text(
                "INSERT INTO posterjob (project_id, user_info_id, status, request_json, "
                "created_at) VALUES (9, :u, 'done', '{}', 0)"), {"u": uid})
            conn.execute(text(
                "INSERT INTO videojob (project_id, user_info_id, kind, status, progress, "
                "request_json, created_at) VALUES (9, :u, 'video', 'done', 1, '{}', 0)"),
                {"u": uid})
            conn.execute(text(
                "INSERT INTO routejob (project_id, user_info_id, project_name, segment_id, "
                "status, params_json, created_at, attempts) "
                "VALUES (9, :u, 'Trip', 's', 'done', '{}', 0, 0)"), {"u": uid})

    command.upgrade(cfg, "head")

    with eng.connect() as conn:
        for table in ("immichtoken", "posterjob", "videojob", "routejob"):
            owners = [r[0] for r in conn.execute(text(f"SELECT user_info_id FROM {table}"))]
            assert owners == [1], f"{table}: {owners}"
    eng.dispose()
