"""Backup management endpoints — list and restore SQLite backups.

A restore replaces the whole server database, so both routes are admin-only
(``require_admin`` re-reads ``is_admin`` from the DB on every call).
"""
from fastapi import APIRouter, Depends, HTTPException, Path

from api.deps import require_admin
from src.backup.backup_service import list_backups, restore_db
from src.utils.logging import get_logger

router = APIRouter(prefix="/api/backup", tags=["backup"])

_log = get_logger(__name__)


@router.get("/", summary="List database backups")
async def get_backups(_admin: dict = Depends(require_admin)) -> list[dict]:
    """Return all available daily SQLite backups, newest-first.

    Each entry is ``{date: "YYYY-MM-DD", size_bytes: int}``. Backups are taken
    automatically every day at 02:00 UTC and retained for 30 days.
    """
    return list_backups()


@router.post("/{date}/restore", summary="Restore a database backup")
async def restore_backup(
    # Backups are named by day; anything that isn't a YYYY-MM-DD date is a 422.
    date: str = Path(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
    current_user: dict = Depends(require_admin),
) -> dict:
    """Restore the database to the backup taken on *date* (YYYY-MM-DD).

    Overwrites the live database with the chosen backup and disposes the
    connection pool so new requests use the restored data. Returns 404 if no
    backup exists for that date. Admin only.
    """
    triggered_by = current_user["sub"]
    _log.info("Backup restore requested: date=%s triggered_by=%s", date, triggered_by)
    try:
        restore_db(date)
    except FileNotFoundError:
        _log.warning("Backup restore failed: date=%s triggered_by=%s reason=not_found", date, triggered_by)
        raise HTTPException(status_code=404, detail=f"No backup found for {date}")
    except Exception as e:
        _log.exception("Backup restore failed: date=%s triggered_by=%s", date, triggered_by)
        raise HTTPException(status_code=500, detail=str(e))
    _log.info("Backup restore succeeded: date=%s triggered_by=%s", date, triggered_by)
    return {"status": "restored", "date": date}
