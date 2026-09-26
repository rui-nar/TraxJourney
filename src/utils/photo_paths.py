"""Where a stored photo name may point on disk.

Memory and journal photos are stored as ``users/<uid>/memories/<id>/<name>.jpg``
(``journal/<id>``) plus ``<name>_thumb.jpg``, and a person's avatar under
``users/<uid>/people/<id>/``. ``<name>`` comes from the database, and the app
only ever makes it itself: ``str(uuid.uuid4())``.

Every file operation on a stored name goes through :func:`photo_file`, which
answers a path only for a name of exactly that form that lands directly inside
the entry's own folder. Anything else names no file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union

#: What the upload endpoints generate: ``str(uuid.uuid4())``.
_PHOTO_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: The two files stored per photo.
PHOTO_SUFFIXES = ("", "_thumb")


def is_photo_name(value: Any) -> bool:
    """True for a name the app makes for a photo it stores."""
    return isinstance(value, str) and _PHOTO_NAME.fullmatch(value) is not None


def photo_folder(data_dir: Union[str, Path], user_id: Union[str, int], kind: str,
                 content_id: Union[str, int]) -> Path:
    """``users/<user_id>/<kind>/<content_id>`` under *data_dir*. Not created."""
    return Path(data_dir) / "users" / str(user_id) / kind / str(content_id)


def photo_file(folder: Path, name: Any, suffix: str = "") -> Optional[Path]:
    """``folder/<name><suffix>.jpg``, or None unless that is a photo file of
    this folder: *name* must be a photo name, and the path must resolve to
    directly inside *folder* (which a link on disk could otherwise defeat).
    """
    if not is_photo_name(name) or suffix not in PHOTO_SUFFIXES:
        return None
    path = folder / f"{name}{suffix}.jpg"
    try:
        if path.resolve().parent != folder.resolve():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def photo_files(folder: Path, names: Iterable[Any]) -> List[Path]:
    """The full-size and thumbnail files of each of *names* in *folder*,
    leaving out any name :func:`photo_file` refuses."""
    return [
        path for name in names for suffix in PHOTO_SUFFIXES
        if (path := photo_file(folder, name, suffix)) is not None
    ]
