"""The one place the database URL is resolved (issue #151).

The app engine (``models.db``), migrations (``alembic/env.py``) and the backup
service each need the URL, and each used to carry its own copy of the default.
They must agree: ``entrypoint.sh`` runs ``alembic upgrade head`` before uvicorn,
so a migration pointed at one file and an app pointed at another would serve an
empty database.

The default file used to be ``viewtripweb.db``. An install that relied on it and
upgrades would otherwise start on a new, empty ``traxjourney.db`` with no error,
so when ``DATABASE_URL`` is unset and the old file sits in the working
directory, resolving refuses outright. That holds even if ``traxjourney.db``
exists too: the migration step creates it before the app ever starts, so its
presence proves nothing about where the data is.
"""
from __future__ import annotations

import os

DEFAULT_DATABASE_URL = "sqlite:///traxjourney.db"

# Relative, like the default: both resolve against the working directory.
_LEGACY_DB_FILE = "viewtripweb.db"


def resolve_database_url() -> str:
    """``DATABASE_URL`` if set (and non-empty), else the default file URL.

    Raises :class:`RuntimeError` when falling back to the default while the old
    default database file exists in the working directory.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    if os.path.exists(_LEGACY_DB_FILE):
        legacy = os.path.abspath(_LEGACY_DB_FILE)
        raise RuntimeError(
            f"Refusing to start: found the old database file {legacy} but "
            "DATABASE_URL is not set. The default database file is now "
            "traxjourney.db, so starting would use a new, empty database. "
            "Either stop the server and rename viewtripweb.db to traxjourney.db "
            "(together with its viewtripweb.db-wal and viewtripweb.db-shm files "
            "if present, and backups/viewtripweb_*.db to backups/traxjourney_*.db), "
            "or set DATABASE_URL=sqlite:///viewtripweb.db to keep using the old file."
        )
    return DEFAULT_DATABASE_URL
