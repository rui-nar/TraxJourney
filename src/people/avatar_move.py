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

Once the moves are done it also deletes the photo files left in a current
member's folder for a person of that trip that no person references any more
(owner decision, pre-U4 leftovers), giving the usage back to the member. A
name any person still holds is never deleted. The owner's folder of a living
person is cleaned the same way but never removed (owner envelope decision,
owner-folder cleanup): a companion who replaced an avatar the owner uploaded
deleted in their own folder, leaving the owner's old file behind. Every other ``people/<id>/`` folder in any user's tree is cleaned the
same way and removed once empty: one whose person no longer exists at all
(person or trip deleted, review U5-R2-1), or one of a living person in a user
who is neither the trip's owner nor a current member, such as an ex-member
(review U5-R3-1).
"""
from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path

from sqlmodel import select

from models.db import get_session
from models.project_db import DBPerson, DBProject, DBProjectMember
from src.billing.usage import record_delta, unlink_and_record
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


def _load() -> tuple[list[tuple[int, str | None, str, list[str]]], set[str], set[int]]:
    """``(person id, avatar name or None, owner dir, member dirs)`` for every
    person whose trip still exists, every avatar name any person holds, and
    the id of every person row, trip or no trip."""
    from api.people import person_owner_dir_id

    with get_session() as sess:
        people = sess.exec(
            select(DBPerson).join(DBProject, DBProject.id == DBPerson.project_id)
        ).all()
        # Every person's, trip or no trip: a name still referenced is never stale.
        names = set(sess.exec(
            select(DBPerson.avatar_photo).where(DBPerson.avatar_photo.is_not(None))
        ).all())
        person_ids = set(sess.exec(select(DBPerson.id)).all())
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
        return out, names, person_ids


def _move_all() -> int:
    # Read everything first: no pooled connection is held through the file work.
    people, current_names, person_ids = _load()
    counts = {"moved": 0, "present": 0, "missing": 0, "conflict": 0, "failed": 0}
    left_in_place: set[str] = set()
    for person_id, name, owner_dir, member_dirs in people:
        if name is None:
            continue
        try:
            outcome = _move_one(person_id, name, owner_dir, member_dirs)
        except Exception:  # noqa: BLE001 — one bad avatar must not stop the rest
            _log.exception("avatar move failed person_id=%s", person_id)
            outcome = "failed"
        if outcome in ("conflict", "failed"):
            left_in_place.add(name)
        counts[outcome] += 1
    # After the moves, so a member's copy of a current avatar is only ever
    # moved, never deleted as stale.
    stale = 0
    keep = current_names | left_in_place
    for person_id, _, _, member_dirs in people:
        for member in member_dirs:
            try:
                stale += _delete_stale(person_id, member, keep)
            except Exception:  # noqa: BLE001 — one bad folder must not stop the rest
                _log.exception("stale avatar cleanup failed person_id=%s member=%s",
                               person_id, member)
    # The owner's folder too, under the same keep rule: after the moves, so an
    # avatar moved in this run is in place and its name in *keep*. Never
    # removed, so the next upload has its folder.
    owner_stale = 0
    for person_id, _, owner_dir, _ in people:
        try:
            owner_stale += _delete_stale(person_id, owner_dir, keep)
        except Exception:  # noqa: BLE001 — one bad folder must not stop the rest
            _log.exception("stale avatar cleanup failed person_id=%s owner=%s",
                           person_id, owner_dir)
    protected = {pid: {owner_dir, *member_dirs}
                 for pid, _, owner_dir, member_dirs in people}
    orphaned = _delete_orphaned(person_ids, protected, keep)
    _log.info("avatar move sweep: moved=%d already_in_place=%d not_found=%d "
              "conflict=%d failed=%d stale_deleted=%d orphan_deleted=%d "
              "owner_stale_deleted=%d",
              counts["moved"], counts["present"], counts["missing"],
              counts["conflict"], counts["failed"], stale, orphaned, owner_stale)
    return counts["moved"]


def _delete_orphaned(person_ids: set[int], protected: dict[int, set[str]],
                     keep: set[str]) -> int:
    """Clean every user's ``people/<id>/`` folder that neither the owner nor a
    current member of the person's trip holds, and remove it once empty.
    Returns how many files were deleted.

    That is a folder whose person row is gone (review U5-R2-1), or a living
    person's folder in any other user's tree, such as an ex-member's (review
    U5-R3-1). The owner's and current members' folders are left to the passes
    above. A living person whose trip is gone has no known
    owner, so all their folders are left alone. Nothing in *keep* is deleted,
    so an ex-member's copy of a current avatar (review R1-2) stays, and the
    name check holds even if SQLite later hands a deleted id to a new person.
    """
    from api import people as people_mod

    users_dir = Path(people_mod._DATA_DIR) / "users"
    if not users_dir.is_dir():
        return 0
    deleted = 0
    for user in sorted(users_dir.iterdir()):
        people_dir = user / "people"
        if not _is_id(user.name) or not people_dir.is_dir():
            continue
        for folder in sorted(people_dir.iterdir()):
            if not _is_id(folder.name) or not folder.is_dir():
                continue
            person_id = int(folder.name)
            if person_id in person_ids and (person_id not in protected
                                            or user.name in protected[person_id]):
                continue
            try:
                deleted += _delete_stale(person_id, user.name, keep)
                if not any(folder.iterdir()):
                    folder.rmdir()
            except Exception:  # noqa: BLE001 — one bad folder must not stop the rest
                _log.exception("orphaned avatar cleanup failed folder=%s", folder)
    return deleted


def _is_id(name: str) -> bool:
    return name.isascii() and name.isdigit()


def _delete_stale(person_id: int, member: str, keep: set[str]) -> int:
    """Delete the photo files in a user's folder for this person that no
    person references any more. Returns how many were deleted.

    Before #470 an avatar lived in its uploader's folder, and replacing or
    removing it from another account deleted in *that* account's folder, so
    the uploader's copy stayed behind, still counted against them. Only names
    the app itself makes are considered (``photo_file``), never one in *keep*.
    """
    from api.people import _avatar_folder

    folder = _avatar_folder(member, person_id)
    if not folder.is_dir():
        return 0
    deleted = 0
    for path in sorted(folder.iterdir()):
        stem = path.name[:-len(".jpg")] if path.name.endswith(".jpg") else None
        if stem is None:
            continue
        name, suffix = (stem[:-len("_thumb")], "_thumb") if stem.endswith("_thumb") \
            else (stem, "")
        if name in keep or photo_file(folder, name, suffix) != path or not path.is_file():
            continue
        unlink_and_record(member, [path])
        _log.info("stale avatar file deleted person_id=%s user=%s file=%s",
                  person_id, member, path.name)
        deleted += 1
    return deleted


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
        if src is None or not src.is_file():
            continue
        if dst is None:
            # Guard (review U5-R1-1): the owner-side path was refused, so this
            # file cannot be moved. Stop before deleting anything.
            raise RuntimeError(f"no owner-side path for person_id={person_id} "
                               f"file={src.name}")
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
