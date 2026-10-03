"""Move companion-uploaded avatars into the trip owner's folder (issue #470).

Before #470 a person's avatar was written to the *uploader's* folder and
charged to the uploader, so an avatar a companion uploaded was invisible to
every other member of the trip. Avatars now live in the trip owner's folder
(``api.people.person_owner_dir_id``); this startup sweep moves the old ones
there once, with the storage usage they carry.

Runs at every API start, so it must be safe to interrupt anywhere and to run
again. Disk is the only record of progress:

- the owner's copy is written to a temporary name, flushed, then renamed into
  place, so it is either absent or complete;
- the member's copies are deleted only once both owner copies match them byte
  for byte, the thumbnail first, so a member's full-size file still present
  means "not finished yet";
- usage moves file by file, right after that file is deleted, by the run that
  deleted it. A crash between the delete and the count loses that one file's
  delta (the nightly reconcile corrects it) but can never count it twice.

Only current members' folders are searched (review R1-2, deferred): an avatar
uploaded by a companion who has since left is reported as found nowhere.
"""
from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path

from sqlmodel import select

from models.db import get_session
from models.project_db import DBPerson, DBProject, DBProjectMember
from src.billing.usage import record_delta
from src.utils.logging import get_logger
from src.utils.photo_paths import PHOTO_SUFFIXES, photo_file

_log = get_logger(__name__)


def move_companion_avatars() -> int:
    """Move every avatar found in a member's folder into its trip owner's
    folder. Returns how many were moved.

    Never raises: a broken sweep must not stop the API from starting. The
    avatars it could not move stay where they are and are retried next start.
    """
    try:
        return _move_all()
    except Exception:  # noqa: BLE001 — a broken sweep must not stop the app booting
        _log.exception("avatar move sweep failed")
        return 0


def _candidates() -> list[tuple[int, str, str, list[str]]]:
    """``(person id, avatar name, owner dir, member dirs)`` for every person
    with an avatar whose trip still exists."""
    from api.people import person_owner_dir_id

    with get_session() as sess:
        people = sess.exec(
            select(DBPerson)
            .join(DBProject, DBProject.id == DBPerson.project_id)
            .where(DBPerson.avatar_photo.is_not(None))
        ).all()
        project_ids = {p.project_id for p in people}
        members: dict[int, list[str]] = {}
        if project_ids:
            for project_id, uid in sess.exec(
                select(DBProjectMember.project_id, DBProjectMember.user_info_id)
                .where(DBProjectMember.project_id.in_(project_ids))
                .order_by(DBProjectMember.user_info_id)
            ).all():
                members.setdefault(project_id, []).append(str(uid))
        out = []
        for p in people:
            owner_dir = person_owner_dir_id(sess, p)
            out.append((p.id, p.avatar_photo, owner_dir,
                        [m for m in members.get(p.project_id, []) if m != owner_dir]))
        return out


def _move_all() -> int:
    # Read everything first: no pooled connection is held through the file work.
    candidates = _candidates()
    counts = {"moved": 0, "present": 0, "missing": 0, "conflict": 0, "failed": 0}
    for person_id, name, owner_dir, member_dirs in candidates:
        try:
            outcome = _move_one(person_id, name, owner_dir, member_dirs)
        except Exception:  # noqa: BLE001 — one bad avatar must not stop the rest
            _log.exception("avatar move failed person_id=%s", person_id)
            outcome = "failed"
        counts[outcome] += 1
    _log.info("avatar move sweep: moved=%d already_in_place=%d not_found=%d "
              "conflict=%d failed=%d", counts["moved"], counts["present"],
              counts["missing"], counts["conflict"], counts["failed"])
    return counts["moved"]


def _move_one(person_id: int, name: str, owner_dir: str, member_dirs: list[str]) -> str:
    from api.people import _avatar_folder

    owner_folder = _avatar_folder(owner_dir, person_id)
    owner_full = photo_file(owner_folder, name)
    if owner_full is None:
        _log.warning("avatar not found person_id=%s: stored name is not a photo name",
                     person_id)
        return "missing"

    holders = []
    for member in member_dirs:
        full = photo_file(_avatar_folder(member, person_id), name)
        if full is not None and full.is_file():
            holders.append(member)
    if not holders:
        if owner_full.is_file():
            return "present"
        _log.warning("avatar not found person_id=%s owner=%s: not in the owner's "
                     "folder nor any current member's", person_id, owner_dir)
        return "missing"
    if len(holders) > 1:
        # One upload writes one folder, so this is not a state the app makes.
        # Leave every copy where it is rather than guess which one is right.
        _log.warning("avatar found in several members' folders person_id=%s "
                     "members=%s: left in place", person_id, holders)
        return "conflict"

    member = holders[0]
    member_folder = _avatar_folder(member, person_id)
    pairs = [(photo_file(member_folder, name, s), photo_file(owner_folder, name, s))
             for s in PHOTO_SUFFIXES]
    owner_folder.mkdir(parents=True, exist_ok=True)
    for src, dst in pairs:
        if src is None or dst is None or not src.is_file():
            continue
        if not dst.is_file():
            _copy_into_place(src, dst)
        if not filecmp.cmp(src, dst, shallow=False):
            _log.warning("avatar copies differ person_id=%s file=%s member=%s owner=%s: "
                         "left in place", person_id, dst.name, member, owner_dir)
            return "conflict"
    # Thumbnail first: the member's full-size file is the marker of a move
    # that has not finished, so it goes last.
    for src, _ in reversed(pairs):
        if src is not None and src.is_file():
            _hand_over(src, member, owner_dir)
    _log.info("avatar moved person_id=%s from_user=%s to_user=%s",
              person_id, member, owner_dir)
    return "moved"


def _copy_into_place(src: Path, dst: Path) -> None:
    """Copy *src* to *dst* so that *dst* is either absent or complete, even
    across a crash or power loss."""
    tmp = dst.with_name(dst.name + ".moving")
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _hand_over(src: Path, from_user: str, to_user: str) -> None:
    """Delete the member's copy and move its bytes from their count to the
    owner's."""
    size = src.stat().st_size
    src.unlink()
    record_delta(from_user, -size)
    record_delta(to_user, size)
