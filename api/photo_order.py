"""Rank-based photo placement for memories and journal entries (issue #237).

A memory's or journal entry's ``photos_json`` is the dense, display-ordered
list of photo UUIDs that every reader consumes. Polarsteps import fires all
of a step's downloads at once, each carrying its intended position
(``order``); manual uploads carry none. The old design used ``order`` as a
list index with ``null`` placeholders, so a manual upload or a delete during
an import got overwritten or shifted.

Here ``order`` is a *rank*, kept beside the list in ``photo_order_json``::

    {"epoch": 0, "ranks": {"<uuid>": 3, ...}}

* A ranked photo is inserted before the first photo whose rank is greater,
  or before the first unranked photo, whichever comes first; equal ranks go
  after each other. Nothing is ever overwritten.
* An unranked photo is appended, so unranked photos sit after every ranked
  one for placement purposes.
* ``ranks`` entries for UUIDs not in the list are ignored (writers that are
  not rank-aware, like archive import, may leave them stale).
* ``epoch`` is bumped by a re-import's clear; a download queued under an
  older epoch is dropped by its caller.

Pure functions over plain values: no DB, no disk, no lock (callers hold
``api.photo_locks.photo_lock``). Inputs are never mutated; every function
returns new objects.
"""
from __future__ import annotations

import json
from typing import Optional, Tuple


def _empty() -> dict:
    return {"epoch": 0, "ranks": {}}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_state(raw: Optional[str]) -> dict:
    """Parse ``photo_order_json``; ``NULL`` or unreadable reads as epoch 0, no ranks.

    Each field is read on its own: a readable epoch survives unreadable
    ranks (and vice versa), and malformed rank entries are dropped one by one,
    so a bad entry never resets the epoch that guards against stale downloads.
    """
    if not raw:
        return _empty()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    epoch = data.get("epoch")
    ranks = data.get("ranks")
    return {
        "epoch": epoch if _is_int(epoch) else 0,
        "ranks": {k: v for k, v in ranks.items() if isinstance(k, str) and _is_int(v)}
        if isinstance(ranks, dict) else {},
    }


def dump_state(state: dict) -> str:
    """Serialize *state* for ``photo_order_json``."""
    return json.dumps({"epoch": state["epoch"], "ranks": state["ranks"]}, sort_keys=True)


def place(photos: list, state: dict, uuid: str,
          rank: Optional[int]) -> Tuple[list, dict]:
    """Add *uuid* to *photos* at the position its *rank* calls for.

    ``rank=None`` appends with no rank. Raises ``ValueError`` if *uuid* is
    already in *photos* (callers place freshly generated UUIDs only).
    """
    if uuid in photos:
        raise ValueError(f"photo {uuid} is already placed")
    ranks = dict(state["ranks"])
    new_photos = list(photos)
    if rank is None:
        ranks.pop(uuid, None)
        new_photos.append(uuid)
    else:
        at = len(new_photos)
        for i, existing in enumerate(new_photos):
            existing_rank = ranks.get(existing)
            if existing_rank is None or existing_rank > rank:
                at = i
                break
        new_photos.insert(at, uuid)
        ranks[uuid] = rank
    return new_photos, {"epoch": state["epoch"], "ranks": ranks}


def remove(photos: list, state: dict, uuid: str) -> Tuple[list, dict]:
    """Drop *uuid* from *photos* and from the ranks (no-op if absent)."""
    ranks = dict(state["ranks"])
    ranks.pop(uuid, None)
    return [p for p in photos if p != uuid], {"epoch": state["epoch"], "ranks": ranks}


def replace(photos: list, state: dict, old: str,
            new: str) -> Optional[Tuple[list, dict]]:
    """Put *new* where *old* is, with *old*'s rank (if any).

    Returns ``None`` when *old* is not in *photos* (deleted meanwhile), so the
    caller can delete the files it just wrote for *new* and answer 404.
    """
    if old not in photos:
        return None
    ranks = dict(state["ranks"])
    new_photos = list(photos)
    new_photos[new_photos.index(old)] = new
    old_rank = ranks.pop(old, None)
    if old_rank is None:
        ranks.pop(new, None)
    else:
        ranks[new] = old_rank
    return new_photos, {"epoch": state["epoch"], "ranks": ranks}


def clear(state: dict) -> dict:
    """State for an emptied photo list: no ranks, epoch bumped.

    The bump makes every download queued before the clear stale.
    """
    return {"epoch": state["epoch"] + 1, "ranks": {}}
