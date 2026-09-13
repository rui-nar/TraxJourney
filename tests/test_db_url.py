"""Database URL resolution and the old-default-file guard (issue #151).

The default database file moved from ``viewtripweb.db`` to ``traxjourney.db``.
``models.db_url.resolve_database_url`` is the single resolver; the app engine,
alembic's ``env.py`` (which ``entrypoint.sh`` runs before uvicorn) and the
backup service must all go through it, or they could disagree about which file
is the database — or one of them could skip the guard.

Every test runs in an empty ``tmp_path`` working directory, because the default
and the guard both resolve against the CWD.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from models.db_url import DEFAULT_DATABASE_URL, resolve_database_url

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return tmp_path


def _touch_legacy(directory: Path) -> None:
    (directory / "viewtripweb.db").write_bytes(b"")


# ── The resolver ──────────────────────────────────────────────────────────────

def test_default_is_traxjourney_db_in_the_working_directory(cwd):
    assert DEFAULT_DATABASE_URL == "sqlite:///traxjourney.db"
    assert resolve_database_url() == "sqlite:///traxjourney.db"


def test_empty_database_url_counts_as_unset(cwd, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    assert resolve_database_url() == DEFAULT_DATABASE_URL


def test_database_url_wins(cwd, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:////data/elsewhere.db")
    assert resolve_database_url() == "sqlite:////data/elsewhere.db"


def test_refuses_when_the_old_default_file_exists(cwd):
    _touch_legacy(cwd)

    with pytest.raises(RuntimeError) as exc:
        resolve_database_url()

    message = str(exc.value)
    assert "viewtripweb.db" in message
    assert "traxjourney.db" in message
    assert "DATABASE_URL" in message
    assert "-wal" in message and "-shm" in message
    assert "backups/viewtripweb_*.db" in message


def test_refuses_even_when_the_new_default_file_also_exists(cwd):
    """alembic creates traxjourney.db before the app starts; it proves nothing."""
    _touch_legacy(cwd)
    (cwd / "traxjourney.db").write_bytes(b"")

    with pytest.raises(RuntimeError):
        resolve_database_url()


def test_no_refusal_when_database_url_is_set(cwd, monkeypatch):
    _touch_legacy(cwd)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///viewtripweb.db")

    assert resolve_database_url() == "sqlite:///viewtripweb.db"


def test_old_file_elsewhere_does_not_refuse(cwd):
    """Only the working directory — where the relative default resolves — counts."""
    (cwd / "sub").mkdir()
    _touch_legacy(cwd / "sub")

    assert resolve_database_url() == DEFAULT_DATABASE_URL


# ── Every entry point goes through it ─────────────────────────────────────────

def _import_models_db(cwd: Path) -> subprocess.CompletedProcess:
    """Import models.db in a fresh interpreter: its engine is built at import."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO), env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-c", "import models.db as d; print(d.engine.url)"],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


def test_app_engine_uses_the_default(cwd):
    result = _import_models_db(cwd)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == DEFAULT_DATABASE_URL


def test_app_engine_refuses_beside_the_old_file(cwd):
    _touch_legacy(cwd)

    result = _import_models_db(cwd)

    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert "viewtripweb.db" in result.stderr


def _alembic_cfg() -> Config:
    cfg = Config(str(_REPO / "alembic.ini"))
    # A decoy: env.py must replace whatever the ini/caller holds with the
    # resolver's answer, or the migration and the app could target different files.
    cfg.set_main_option("sqlalchemy.url", "sqlite:///decoy.db")
    return cfg


def test_alembic_env_uses_the_default(cwd):
    command.current(_alembic_cfg())

    assert (cwd / "traxjourney.db").exists()
    assert not (cwd / "decoy.db").exists()


def test_alembic_env_refuses_beside_the_old_file(cwd):
    """The entrypoint.sh path: `alembic upgrade head` runs before uvicorn."""
    _touch_legacy(cwd)

    with pytest.raises(RuntimeError, match="viewtripweb.db"):
        command.current(_alembic_cfg())

    assert not (cwd / "traxjourney.db").exists()


def test_backup_service_uses_the_default(cwd):
    from src.backup import backup_service

    assert backup_service._db_path() == Path("traxjourney.db")


def test_backup_service_refuses_beside_the_old_file(cwd):
    from src.backup import backup_service

    _touch_legacy(cwd)

    with pytest.raises(RuntimeError):
        backup_service._db_path()
