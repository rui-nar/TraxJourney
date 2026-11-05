"""Rank-based photo placement for memories and journal entries (issue #237).

A memory's or journal entry's ``photos_json`` is the dense, display-ordered
list of photo UUIDs that every reader consumes. Polarsteps import fires all
of a step's downloads at once, each carrying its intended position
(``order``); manual uploads carry none. The old design used ``order`` as a
list index with ``null`` placeholders, so a manual upload or a delete during
an import got overwritten or shifted.

Here ``order`` is a *rank*, kept beside the list in ``photo_order_json``::

    {"epoch": 0, "ranks": {"<uuid>": 3, ...}, "hashes": {"<uuid>": "<sha256>", ...},
     "sources": {"<uuid>": "immich:<asset id>", ...}}

* A ranked photo is inserted before the first photo whose rank is greater,
  or before the first unranked photo, whichever comes first; equal ranks go
  after each other. Nothing is ever overwritten.
* An unranked photo is appended, so unranked photos sit after every ranked
  one for placement purposes.
* ``ranks`` entries for UUIDs not in the list are ignored (writers that are
  not rank-aware, like archive import, may leave them stale).
* ``epoch`` is bumped by a re-import's clear; a download queued under an
  older epoch is dropped by its caller.
* ``hashes`` holds the sha256 (64 lower-case hex) of each photo downloaded
  from a URL, so a repeated import can tell the memory already has it (issue
  #566). A hash follows its photo: ``place`` records it, ``remove`` drops it,
  ``replace`` hands it to the replacement and ``clear`` empties it. As with
  ranks, entries for UUIDs not in the list are ignored (``has_hash``). Every
  writer passes a full state, so writers that only change the order keep
  the hashes. ``fill_hashes`` records hashes for listed photos that have
  none yet (a memory's photos before a Polarsteps adopt-and-refresh), and
  never overwrites one.
* ``sources`` names where a photo came from, ``"immich:<asset id>"`` for an
  original taken from an Immich album (issue #33). ``place`` records it,
  ``remove`` drops it and ``clear`` empties it. Unlike a hash, ``replace``
  never hands it to the replacement: the new photo gets the source given
  for it, or none. Entries for UUIDs not in the list are ignored
  (``source_of``).

Pure functions over plain values: no DB, no disk, no lock (callers hold
``api.photo_locks.photo_lock``). Inputs are never mutated; every function
returns new objects.
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_SOURCE = re.compile(r"immich:[A-Za-z0-9-]{1,64}")


def _empty() -> dict:
    return {"epoch": 0, "ranks": {}, "hashes": {}, "sources": {}}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hash(value) -> bool:
    return isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None


def _is_source(value) -> bool:
    return isinstance(value, str) and _SOURCE.fullmatch(value) is not None


def load_state(raw: Optional[str]) -> dict:
    """Parse ``photo_order_json``; ``NULL`` or unreadable reads as epoch 0 and nothing else.

    Each field is read on its own: a readable epoch survives unreadable
    ranks, hashes or sources (and vice versa), and malformed rank, hash and
    source entries are dropped one by one, so a bad entry never resets the
    epoch that guards against stale downloads. A state written before hashes
    or sources existed reads as no hashes or no sources.
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
    hashes = data.get("hashes")
    sources = data.get("sources")
    return {
        "epoch": epoch if _is_int(epoch) else 0,
        "ranks": {k: v for k, v in ranks.items() if isinstance(k, str) and _is_int(v)}
        if isinstance(ranks, dict) else {},
        "hashes": {k: v for k, v in hashes.items() if isinstance(k, str) and _is_hash(v)}
        if isinstance(hashes, dict) else {},
        "sources": {k: v for k, v in sources.items() if isinstance(k, str) and _is_source(v)}
        if isinstance(sources, dict) else {},
    }


def dump_state(state: dict) -> str:
    """Serialize *state* for ``photo_order_json``."""
    return json.dumps(
        {"epoch": state["epoch"], "ranks": state["ranks"], "hashes": state["hashes"],
         "sources": state["sources"]},
        sort_keys=True,
    )


def place(photos: list, state: dict, uuid: str, rank: Optional[int],
          content_hash: Optional[str] = None,
          source: Optional[str] = None) -> Tuple[list, dict]:
    """Add *uuid* to *photos* at the position its *rank* calls for.

    ``rank=None`` appends with no rank. *content_hash* (the sha256 of a
    downloaded photo) and *source* are recorded for *uuid*; ``None`` records
    none. Raises ``ValueError`` if *uuid* is already in *photos* (callers
    place freshly generated UUIDs only).
    """
    if uuid in photos:
        raise ValueError(f"photo {uuid} is already placed")
    ranks = dict(state["ranks"])
    hashes = dict(state["hashes"])
    if content_hash is None:
        hashes.pop(uuid, None)
    else:
        hashes[uuid] = content_hash
    sources = dict(state["sources"])
    if source is None:
        sources.pop(uuid, None)
    else:
        sources[uuid] = source
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
    return new_photos, {"epoch": state["epoch"], "ranks": ranks, "hashes": hashes,
                        "sources": sources}


def remove(photos: list, state: dict, uuid: str) -> Tuple[list, dict]:
    """Drop *uuid* from *photos*, the ranks, the hashes and the sources (no-op if absent)."""
    ranks = dict(state["ranks"])
    ranks.pop(uuid, None)
    hashes = dict(state["hashes"])
    hashes.pop(uuid, None)
    sources = dict(state["sources"])
    sources.pop(uuid, None)
    return [p for p in photos if p != uuid], {"epoch": state["epoch"], "ranks": ranks, "hashes": hashes,
                                              "sources": sources}


def replace(photos: list, state: dict, old: str,
            new: str, source: Optional[str] = None) -> Optional[Tuple[list, dict]]:
    """Put *new* where *old* is, with *old*'s rank and hash (if any).

    The hash names the download *old* stood for, so a re-import does not
    bring back an original the owner replaced. *old*'s source is never
    carried: *new* gets *source*, or none when it is ``None``.

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
    hashes = dict(state["hashes"])
    old_hash = hashes.pop(old, None)
    if old_hash is None:
        hashes.pop(new, None)
    else:
        hashes[new] = old_hash
    sources = dict(state["sources"])
    sources.pop(old, None)
    if source is None:
        sources.pop(new, None)
    else:
        sources[new] = source
    return new_photos, {"epoch": state["epoch"], "ranks": ranks, "hashes": hashes,
                        "sources": sources}


def clear(state: dict) -> dict:
    """State for an emptied photo list: no ranks, hashes or sources, epoch bumped.

    The bump makes every download queued before the clear stale.
    """
    return {"epoch": state["epoch"] + 1, "ranks": {}, "hashes": {}, "sources": {}}


def fill_hashes(photos: list, state: dict, hashes: dict) -> dict:
    """Record ``hashes[uuid]`` for each UUID in *photos* that has no hash yet.

    A hash already recorded is never overwritten. UUIDs not in *photos* and
    values that are not a sha256 (64 lower-case hex) are ignored. The epoch,
    ranks and sources are left as they are.
    """
    filled = dict(state["hashes"])
    for uuid in photos:
        if uuid not in filled and _is_hash(hashes.get(uuid)):
            filled[uuid] = hashes[uuid]
    return {"epoch": state["epoch"], "ranks": dict(state["ranks"]), "hashes": filled,
            "sources": dict(state["sources"])}


def has_hash(state: dict, photos: list, content_hash: str) -> bool:
    """True when a photo currently in *photos* carries *content_hash*.

    A stale entry for a UUID no longer in *photos* does not count, and a
    missing or malformed *content_hash* never matches (an unhashed photo
    must not make every unhashed download look present).
    """
    if not _is_hash(content_hash):
        return False
    hashes = state["hashes"]
    return any(hashes.get(uuid) == content_hash for uuid in photos)


def source_of(state: dict, photos: list, uuid: str) -> Optional[str]:
    """The source recorded for *uuid*, or ``None``.

    A stale entry for a UUID no longer in *photos* does not count.
    """
    if uuid not in photos:
        return None
    return state["sources"].get(uuid)
