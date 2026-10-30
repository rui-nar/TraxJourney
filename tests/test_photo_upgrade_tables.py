"""Photo-upgrade scan, album-photo and suggestion tables (issue #33, D8).

The three tables go with their trip and with the account of the person who
ran the scan, and a person has at most one active scan per trip.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.billing  # noqa: F401 — registers every table on the metadata
import models.db as db_module
import src.admin.storage as storage_mod
from models.project_db import (
    DBPhotoUpgradeAsset,
    DBPhotoUpgradeScan,
    DBPhotoUpgradeSuggestion,
    DBProject,
)
from models.user import LocalUser, UserInfo
from src.auth.account_deletion import delete_user_and_data
from src.project.project_repo import ProjectRepo


@pytest.fixture
def engine(monkeypatch, tmp_path):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
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


def _mk_project(sess, owner: int, name: str) -> int:
    proj = DBProject(user_info_id=owner, name=name)
    sess.add(proj)
    sess.commit()
    return proj.id


def _mk_scan(sess, project_id: int, user_id: int, status: str = "ready") -> int:
    """A scan with one album photo and one suggestion."""
    scan = DBPhotoUpgradeScan(project_id=project_id, user_info_id=user_id,
                              album_id="album", status=status)
    sess.add(scan)
    sess.commit()
    sess.add(DBPhotoUpgradeAsset(scan_id=scan.id, asset_id="a1",
                                 local_date="2026-05-01"))
    sess.add(DBPhotoUpgradeSuggestion(
        scan_id=scan.id, artifact_kind="memory", artifact_id=1,
        photo_uuid="u1", position=0, date="2026-05-01"))
    sess.commit()
    return scan.id


def _scan_ids(sess, model) -> set[int]:
    return set(sess.exec(select(model.scan_id)).all())


def _scans(sess) -> set[int]:
    return set(sess.exec(select(DBPhotoUpgradeScan.id)).all())


def test_deleting_a_project_removes_its_scans_assets_and_suggestions(engine):
    with Session(engine) as sess:
        owner = _mk_user(sess, "owner@x.io")
        companion = _mk_user(sess, "comp@x.io")
        trip = _mk_project(sess, owner, "Trip")
        other = _mk_project(sess, owner, "Other")
        gone = {_mk_scan(sess, trip, owner), _mk_scan(sess, trip, companion)}
        kept = _mk_scan(sess, other, owner)

    with Session(engine) as sess:
        assert ProjectRepo().delete_project(sess, owner, "Trip") is True

    with Session(engine) as sess:
        assert _scans(sess) == {kept}
        assert _scan_ids(sess, DBPhotoUpgradeAsset) == {kept}
        assert _scan_ids(sess, DBPhotoUpgradeSuggestion) == {kept}
        assert not gone & _scans(sess)


def test_deleting_an_account_removes_its_scans_in_other_peoples_projects(engine):
    with Session(engine) as sess:
        owner = _mk_user(sess, "owner@x.io")
        companion = _mk_user(sess, "comp@x.io")
        trip = _mk_project(sess, owner, "Trip")
        own_trip = _mk_project(sess, companion, "Mine")
        _mk_scan(sess, trip, companion)
        _mk_scan(sess, own_trip, companion)
        # The owner's scan of the companion's trip goes with that trip.
        _mk_scan(sess, own_trip, owner)
        kept = _mk_scan(sess, trip, owner)

    with Session(engine) as sess:
        delete_user_and_data(sess, companion)

    with Session(engine) as sess:
        assert _scans(sess) == {kept}
        assert _scan_ids(sess, DBPhotoUpgradeAsset) == {kept}
        assert _scan_ids(sess, DBPhotoUpgradeSuggestion) == {kept}


@pytest.mark.parametrize("first", ["pending", "scanning"])
@pytest.mark.parametrize("second", ["pending", "scanning"])
def test_a_second_active_scan_is_refused(engine, first, second):
    with Session(engine) as sess:
        uid = _mk_user(sess, "a@x.io")
        pid = _mk_project(sess, uid, "Trip")
        _mk_scan(sess, pid, uid, status=first)
        with pytest.raises(IntegrityError):
            _mk_scan(sess, pid, uid, status=second)


@pytest.mark.parametrize("finished", ["ready", "failed"])
def test_an_active_scan_beside_a_finished_one_is_allowed(engine, finished):
    with Session(engine) as sess:
        uid = _mk_user(sess, "a@x.io")
        other = _mk_user(sess, "b@x.io")
        pid = _mk_project(sess, uid, "Trip")
        _mk_scan(sess, pid, uid, status=finished)
        _mk_scan(sess, pid, uid, status=finished)
        _mk_scan(sess, pid, uid, status="pending")
        # Another person's active scan of the same trip is theirs.
        _mk_scan(sess, pid, other, status="scanning")
        assert len(_scans(sess)) == 4


def test_the_migration_refuses_a_second_active_scan(tmp_path, monkeypatch):
    """The partial unique index is created by the migration too, not only by
    the model metadata."""
    db_path = tmp_path / "scans.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    insert = text(
        "INSERT INTO photoupgradescan (project_id, user_info_id, album_id, status) "
        "VALUES (1, 1, 'album', :s)")
    eng = create_engine(url)
    try:
        with eng.begin() as conn:
            conn.execute(insert, {"s": "ready"})
            conn.execute(insert, {"s": "pending"})
        with pytest.raises(IntegrityError):
            with eng.begin() as conn:
                conn.execute(insert, {"s": "scanning"})
    finally:
        eng.dispose()
