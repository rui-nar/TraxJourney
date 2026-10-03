"""Reading an uploaded trip ZIP into a trip and staged photos (#469).

The archive is untrusted: any signed-in user can upload one, crafted or not.
:func:`read_trip_zip` therefore

- refuses an archive whose central directory is too large before
  :class:`zipfile.ZipFile` loads it into memory;
- reads only the entries it looks for, by exact name (Decision 6): the one
  root-level trip file, and ``photos/{id}/{uuid}.jpg`` or
  ``journal/{id}/{uuid}.jpg`` for each photo the trip file lists. Every other
  entry, ``../`` and absolute names included, is ignored, and no entry name is
  ever joined to a filesystem path;
- takes an export unzipped and zipped again (Finder "Compress", Explorer
  "Compress folder") as it was: when every entry sits under one top-level
  folder, that folder is the root. Finder's ``__MACOSX/`` entries and
  AppleDouble ``._X`` files beside their ``X`` are left out when finding it;
- reads stored and deflated entries only, the two methods an export, Finder
  and Explorer write, and refuses any other method before opening the entry;
- counts the bytes it inflates instead of trusting the sizes the archive
  declares;
- decodes each photo like an upload (src/utils/photo_store.py), one at a time,
  and writes it with a fresh thumbnail into the staging directory.

Every fault of the archive raises :class:`InvalidTripArchive`, whose message a
caller can show the uploader. When it is raised no photo or thumbnail is left
in the staging directory; its ``manifest.json`` may remain, since the caller
removes the whole directory.
"""
from __future__ import annotations

import json
import shutil
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, BinaryIO, Dict, List, Tuple

from src.brand import APP_NAME
from src.models.project import Project
from src.project.project_io import InvalidProjectFile, ProjectIO
from src.project.staged_photos import StagedPhoto, StagedPhotos
from src.utils.photo_paths import is_photo_name
from src.utils.photo_store import MAX_PHOTO_BYTES, InvalidPhoto, write_photo_files

#: Most entries an archive may hold. An export holds one trip file plus two
#: entries at most per photo it carries (only the full file today), so 20,000
#: is room for a trip with well over ten thousand photos. Checked against the
#: end-of-central-directory record before the archive is opened, then against
#: the entries actually read.
MAX_ENTRIES = 20_000

#: Largest central directory, in bytes, that is loaded. ``ZipFile()`` reads the
#: whole directory into memory, and one object per entry, before any count
#: could be checked; and it reads entries until this many bytes are consumed,
#: whatever count the end record claims. A directory entry is 46 bytes plus its
#: name and extra fields: about 150 bytes for an export's
#: ``journal/<id>/<uuid>.jpg``, so 512 bytes each leaves room for the extra
#: fields other zip tools add. 20,000 × 512 bytes is 10 MB.
MAX_CENTRAL_DIRECTORY_BYTES = MAX_ENTRIES * 512

#: Largest trip file, uncompressed. The same limit as a ``.traxj`` upload
#: (``MAX_IMPORT_BYTES`` in api/project_transfer.py, whose comment gives the
#: memory budget): the trip file is parsed in memory exactly as that upload is.
MAX_TRIP_FILE_BYTES = 50 * 1024 * 1024

#: Most photo bytes, uncompressed, one archive may stage. Photos are already
#: compressed, so a real export inflates to about its own size, and the upload
#: is at most 1 GB. Without this, 20,000 entries that each inflate to the
#: per-photo limit would stage 500 GB on the data volume before the storage
#: quota is checked on what was staged.
MAX_STAGED_PHOTO_BYTES = 1024 * 1024 * 1024

#: Name of the file the stager writes first into its staging directory, so a
#: directory left behind by a crash can be traced to its import.
MANIFEST_NAME = "manifest.json"

#: Where each kind's photos are in the archive, by photo_folder kind.
_ARCHIVE_FOLDERS = {"memories": "photos", "journal": "journal"}

_CHUNK = 64 * 1024

#: The compression methods an entry that is read may use: what an export,
#: Finder and Explorer write. Each other method is a decompressor with errors
#: of its own (bzip2, LZMA, Zstandard ...), so it is refused before the entry
#: is opened rather than caught one by one.
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

#: What zipfile raises for a corrupt, truncated or unsupported stored or
#: deflated entry: ValueError (UnicodeDecodeError) is a local header name
#: flagged UTF-8 that isn't. Caught around the zipfile calls only, so a disk
#: error writing a staged photo is not mistaken for a fault of the archive.
_ENTRY_ERRORS = (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error,
                 ValueError)


def _is_os_litter(name: str, names: AbstractSet[str]) -> bool:
    """Whether *name*, among the archive's *names*, is metadata a desktop zip
    tool adds: anything in macOS's ``__MACOSX/`` folder, or an AppleDouble
    ``._X`` beside its file ``X``. A ``._X`` alone is a file of its own: a trip
    named "._Alps" exports as ``._Alps.traxj``."""
    if name.startswith("__MACOSX/"):
        return True
    folder, _, base = name.rpartition("/")
    if not base.startswith("._"):
        return False
    return (f"{folder}/{base[2:]}" if folder else base[2:]) in names


def _root(names: AbstractSet[str]) -> str:
    """The prefix the export's entries sit under: ``""`` when a trip file is at
    the top level, else ``"P/"`` when every entry (OS litter aside) is under
    the one top-level folder ``P``, as when an unzipped export is zipped again.
    Otherwise ``""``, where no trip file will be found."""
    kept = [n for n in names if not _is_os_litter(n, names)]
    if any("/" not in n and n.endswith(ProjectIO.EXTENSION) for n in kept):
        return ""
    if not kept or any("/" not in n for n in kept):
        return ""
    tops = {n.split("/", 1)[0] for n in kept}
    if len(tops) != 1:
        return ""
    (top,) = tops
    # "/x" is absolute, "./x" and "../x" relative: never a folder to read from.
    return "" if top in ("", ".", "..") else top + "/"


class InvalidTripArchive(ValueError):
    """The upload is not a trip archive that can be imported. The message is
    a sentence for the uploader, naming what is wrong."""


def _check_end_record(fileobj: BinaryIO) -> None:
    """Refuse an archive whose end-of-central-directory record, ZIP64 included,
    declares too many entries or too large a directory.

    Reads the record with the function ``ZipFile`` itself uses, so both see the
    same record: a parser of our own could be made to disagree with it. It is
    private to zipfile; a change to it fails here loudly (a server error), never
    silently past the check.
    """
    try:
        endrec = zipfile._EndRecData(fileobj)
    except (zipfile.BadZipFile, OSError):
        endrec = None
    if not endrec:
        raise InvalidTripArchive("This file is not a ZIP archive, or it is damaged.")
    if endrec[zipfile._ECD_ENTRIES_TOTAL] > MAX_ENTRIES:
        raise InvalidTripArchive(
            f"This archive holds too many files. The limit is {MAX_ENTRIES:,}.")
    if endrec[zipfile._ECD_SIZE] > MAX_CENTRAL_DIRECTORY_BYTES:
        raise InvalidTripArchive("This archive's table of contents is too large.")


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, what: str) -> bytes:
    """The bytes of *info*, refused once more than *limit* have been inflated,
    whatever size the archive declares."""
    too_large = InvalidTripArchive(
        f"The {what} in this archive is too large. The limit is {limit // (1024 * 1024)} MB.")
    if info.compress_type not in _METHODS:
        method = zipfile.compressor_names.get(info.compress_type, f"method {info.compress_type}")
        raise InvalidTripArchive(
            f"This archive uses an unsupported compression method ({method}). "
            "Re-create it as a standard ZIP.")
    if info.file_size > limit:
        raise too_large
    chunks: List[bytes] = []
    total = 0
    try:
        with zf.open(info) as f:
            while chunk := f.read(_CHUNK):
                total += len(chunk)
                if total > limit:
                    raise too_large
                chunks.append(chunk)
    except InvalidTripArchive:
        raise  # too large: a ValueError too, but not "damaged"
    except _ENTRY_ERRORS:
        raise InvalidTripArchive(f"The {what} in this archive is damaged or unreadable.") from None
    return b"".join(chunks)


def _write_manifest(staging_dir: Path, importer: int, trip_name: str) -> None:
    manifest = {
        "importer": importer,
        "trip_name": trip_name,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    (staging_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def _photo_lists(project: Project) -> List[Tuple[str, int, List[str]]]:
    """``(kind, file item id, photos)`` for each memory and journal entry that
    has an id to look its photos up by."""
    lists = []
    for item in project.items:
        entry = item.memory if item.item_type == "memory" else (
            item.journal if item.item_type == "journal" else None)
        if entry is None:
            continue
        kind = "memories" if item.item_type == "memory" else "journal"
        if isinstance(entry.id, int) and not isinstance(entry.id, bool):
            lists.append((kind, entry.id, list(entry.photos or [])))
    return lists


def _stage(zf: zipfile.ZipFile, entries: Dict[str, zipfile.ZipInfo], root: str,
           project: Project, staging_dir: Path) -> StagedPhotos:
    staged: StagedPhotos = {}
    inflated = 0
    for kind, item_id, photos in _photo_lists(project):
        for name in photos:
            # Only a name the app makes is looked up, so the entry name built
            # from it is plain; and the name is never used as a path itself.
            if not is_photo_name(name) or name in staged.get((kind, item_id), {}):
                continue
            entry_name = f"{root}{_ARCHIVE_FOLDERS[kind]}/{item_id}/{name}.jpg"
            info = entries.get(entry_name)
            if info is None:
                continue  # missing from the archive: ingest drops the name
            raw = _read_entry(zf, info, MAX_PHOTO_BYTES, f"photo {entry_name}")
            inflated += len(raw)
            if inflated > MAX_STAGED_PHOTO_BYTES:
                raise InvalidTripArchive(
                    f"The photos in this archive are too large together. The limit is "
                    f"{MAX_STAGED_PHOTO_BYTES // (1024 * 1024 * 1024)} GB.")
            try:
                full, thumb = write_photo_files(staging_dir / kind / str(item_id), name, raw)
            except InvalidPhoto as exc:
                raise InvalidTripArchive(
                    f"The photo {entry_name} in this archive can't be imported: {exc}.") from None
            del raw
            staged.setdefault((kind, item_id), {})[name] = StagedPhoto(
                full=full, thumb=thumb, bytes=full.stat().st_size + thumb.stat().st_size)
    return staged


def read_trip_zip(fileobj: BinaryIO, staging_dir: Path, *, importer: int,
                  trip_name: str) -> Tuple[Project, StagedPhotos]:
    """Read the trip archive *fileobj* (seekable) into its trip, staging each
    photo it carries under *staging_dir*.

    Writes ``manifest.json`` (*importer*, *trip_name*, created time) into
    *staging_dir* first. Returns the trip and its staged photos, keyed by
    ``(kind, the item's id in the trip file)`` then by photo name. A photo the
    trip file lists but the archive lacks is simply not staged.

    Raises :class:`InvalidTripArchive` for any fault of the archive, leaving no
    staged photo behind.
    """
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(staging_dir, importer, trip_name)
    _check_end_record(fileobj)
    fileobj.seek(0)
    try:
        zf = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError, OSError,
            ValueError):
        # ValueError: a directory name flagged UTF-8 that isn't (UnicodeDecodeError).
        raise InvalidTripArchive("This file is not a ZIP archive, or it is damaged.") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise InvalidTripArchive(
                f"This archive holds too many files. The limit is {MAX_ENTRIES:,}.")
        entries: Dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.filename in entries:
                # Which of two same-named entries is "the" file is up to the
                # reader; an export never has two, so neither is taken.
                raise InvalidTripArchive(
                    "This archive holds two files with the same name, so it can't be imported.")
            entries[info.filename] = info

        names = entries.keys()
        root = _root(names)
        trip_files = [n for n in names if n.startswith(root) and not _is_os_litter(n, names)
                      and "/" not in n[len(root):] and n.endswith(ProjectIO.EXTENSION)]
        if not trip_files:
            raise InvalidTripArchive(
                f"This archive holds no {APP_NAME} trip file ({ProjectIO.EXTENSION}) at its top level.")
        if len(trip_files) > 1:
            raise InvalidTripArchive(
                f"This archive holds more than one trip file ({ProjectIO.EXTENSION}) at its top level.")
        raw = _read_entry(zf, entries[trip_files[0]], MAX_TRIP_FILE_BYTES, "trip file")
        try:
            project = ProjectIO.from_bytes(raw)
        except InvalidProjectFile as exc:
            raise InvalidTripArchive(
                f"The trip file in this archive isn't a valid {APP_NAME} trip: {exc}.") from None
        del raw

        try:
            staged = _stage(zf, entries, root, project, staging_dir)
        except BaseException:
            # Nothing staged survives a refused archive (or any other failure):
            # only the manifest stays, for the caller to remove with the
            # directory.
            for kind in _ARCHIVE_FOLDERS:
                shutil.rmtree(staging_dir / kind, ignore_errors=True)
            raise
    return project, staged
