"""One-to-one matching of trip photos to album photos (issue #33, decision D7).

Pure functions over plain values: no I/O, no DB, no Immich calls (the model
to follow is ``api/photo_order.py``). A trip photo and an album photo are
compared by the summed Hamming distance of their aHash and dHash (0-128).

How a scan decides, in order:

1. A photo with a ``source`` is already an original: ``already_original``,
   and its asset leaves the pool.
2. Trip photos with the same non-None ``content_hash`` are exact copies and
   are matched as one unit; every copy gets the same asset. A photo with no
   ``content_hash`` is never grouped.
3. Candidates are assets whose local day is within one day of the photo's
   (a hard filter), with the same upright aspect ratio within
   ``ASPECT_TOLERANCE`` (a hard gate). A pair is *plausible*, and may be
   proposed, when its score is at most ``MAX_SCORE``.
4. Plausible pairs are taken in ascending order of (score, same day first,
   distance between the two places, ids); each photo unit and each asset is
   used once. Location only breaks ties; it never filters.
5. Confidence is ``low`` when another asset scores within
   ``AMBIGUITY_MARGIN`` of the chosen one, or when another photo unit
   scores that close for the chosen asset. Rivals count whatever their score
   (a burst frame just over ``MAX_SCORE`` is still a rival); they only need to
   pass the gates and not be excluded. An excluded (rejected) asset is never
   proposed, but it is still a rival: a frame the user turned down that scores
   within the margin of the proposal makes the proposal ``low``.
6. An asset with no more pixels than the current photo is
   ``already_original`` rather than a proposal.

A photo that lost its asset to a different photo and has nothing else is
``no_match`` with ``low`` confidence and the lost asset among its
alternatives, so the review can still offer it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .hashing import Fingerprint

# Calibrated on the owner's real album (OD2): true matches scored 0-2, the
# closest wrong candidate 33, typical ones 40-60.
MAX_SCORE = 12
AMBIGUITY_MARGIN = 10
#: Largest relative difference of upright width/height between two photos.
ASPECT_TOLERANCE = 0.02
#: Pairs scoring up to this are kept: a rival of a proposal (score <= MAX_SCORE)
#: is within the margin of it, so nothing scoring higher can matter.
_KEEP_SCORE = MAX_SCORE + AMBIGUITY_MARGIN
#: Alternatives kept on a suggestion.
MAX_ALTERNATIVES = 5

PROPOSED = "proposed"
NO_MATCH = "no_match"
ALREADY_ORIGINAL = "already_original"
HIGH = "high"
LOW = "low"

_SOURCE_PREFIX = "immich:"
_FAR = math.inf


@dataclass(frozen=True)
class TripPhoto:
    key: str
    date: date
    lat: Optional[float]
    lon: Optional[float]
    fp: Optional[Fingerprint]
    pixels: Optional[int]
    source: Optional[str]
    content_hash: Optional[str]


@dataclass(frozen=True)
class AlbumPhoto:
    id: str
    local_date: date
    lat: Optional[float]
    lon: Optional[float]
    fp: Optional[Fingerprint]
    pixels: Optional[int]


@dataclass(frozen=True)
class Suggestion:
    key: str
    state: str                     # proposed | no_match | already_original
    asset_id: Optional[str]
    score: Optional[int]
    confidence: Optional[str]      # high | low | None
    alternatives: Tuple[Tuple[str, int], ...] = ()  # (asset id, score), <= 5


@dataclass(frozen=True)
class _Pair:
    asset_id: str
    score: int
    day_off: int      # 0 same day, 1 adjacent
    distance: float   # km, inf when a location is missing


def _order(p: _Pair):
    return (p.score, p.day_off, p.distance, p.asset_id)


def _combined(fp: Fingerprint) -> int:
    return fp.ahash | (fp.dhash << 64)


def _aspect(fp: Fingerprint) -> float:
    return fp.width / fp.height


def _aspect_ok(a: float, b: float) -> bool:
    return abs(a - b) <= ASPECT_TOLERANCE * max(a, b)


def _usable(fp: Optional[Fingerprint]) -> bool:
    return fp is not None and fp.width > 0 and fp.height > 0


def _km(lat1, lon1, lat2, lon2) -> float:
    if None in (lat1, lon1, lat2, lon2):
        return _FAR
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742.0 * math.asin(min(1.0, math.sqrt(h)))


class _Pool:
    """Album photos with a usable fingerprint, bucketed by local day."""

    def __init__(self, album: Iterable[AlbumPhoto], skip: Set[str]):
        self.by_day: Dict[int, List[Tuple[str, int, float, AlbumPhoto]]] = {}
        for a in album:
            if not _usable(a.fp) or a.id in skip:
                continue
            self.by_day.setdefault(a.local_date.toordinal(), []).append(
                (a.id, _combined(a.fp), _aspect(a.fp), a))

    def candidates(self, dates: Sequence[date], fp: Fingerprint, lat, lon,
                   max_score: Optional[int]) -> List[_Pair]:
        """Gated pairs for one photo unit (possibly several dates), best first."""
        mine, ratio = _combined(fp), _aspect(fp)
        best: Dict[str, _Pair] = {}
        for d in dates:
            o = d.toordinal()
            for off in (0, 1, -1):
                for aid, comb, asp, a in self.by_day.get(o + off, ()):
                    score = (mine ^ comb).bit_count()
                    if max_score is not None and score > max_score:
                        continue
                    if not _aspect_ok(ratio, asp):
                        continue
                    pair = _Pair(aid, score, abs(off), _km(lat, lon, a.lat, a.lon))
                    old = best.get(aid)
                    if old is None or _order(pair) < _order(old):
                        best[aid] = pair
        return sorted(best.values(), key=_order)


def rank_candidates(photo: TripPhoto,
                    album_photos: Iterable[AlbumPhoto]) -> List[Tuple[str, int]]:
    """Every album photo that passes the day filter and the aspect gate,
    best first, as ``(asset id, score)``. Unlike a proposal there is no score
    cap: this feeds the "Choose another" chooser."""
    if not _usable(photo.fp):
        return []
    pool = _Pool(album_photos, set())
    return [(p.asset_id, p.score) for p in
            pool.candidates([photo.date], photo.fp, photo.lat, photo.lon, None)]


def match(photos: Sequence[TripPhoto], album_photos: Iterable[AlbumPhoto],
          excluded_pairs: Iterable[Tuple[str, str]] = ()) -> List[Suggestion]:
    """One Suggestion per trip photo, ordered by key.

    ``excluded_pairs`` are ``(photo key, asset id)`` pairs never proposed
    (rejections from earlier scans). A group of exact copies skips an asset
    any of its copies has rejected.
    """
    album = list(album_photos)
    pixels_of = {a.id: a.pixels for a in album}
    excluded = set(excluded_pairs)
    out: Dict[str, Suggestion] = {}

    # 1. Photos that already came from the album.
    taken: Set[str] = set()
    rest: List[TripPhoto] = []
    for p in sorted(photos, key=lambda x: x.key):
        if p.source:
            asset = (p.source[len(_SOURCE_PREFIX):]
                     if p.source.startswith(_SOURCE_PREFIX) else p.source)
            taken.add(asset)
            out[p.key] = Suggestion(p.key, ALREADY_ORIGINAL, asset, None, None)
        elif not _usable(p.fp):
            out[p.key] = Suggestion(p.key, NO_MATCH, None, None, None)
        else:
            rest.append(p)

    # 2. Exact copies are one unit.
    units: List[List[TripPhoto]] = []
    by_hash: Dict[str, List[TripPhoto]] = {}
    for p in rest:
        if p.content_hash is None:
            units.append([p])
        elif p.content_hash in by_hash:
            by_hash[p.content_hash].append(p)
        else:
            by_hash[p.content_hash] = [p]
            units.append(by_hash[p.content_hash])

    # 3. Plausible pairs per unit.
    pool = _Pool(album, taken)
    cands: List[List[_Pair]] = []
    rejected_scores: List[List[int]] = []
    for u in units:
        loc = next(((m.lat, m.lon) for m in u if m.lat is not None and m.lon is not None),
                   (None, None))
        pairs = pool.candidates(sorted({m.date for m in u}), u[0].fp, loc[0], loc[1],
                                _KEEP_SCORE)
        out_of_play = [any((m.key, q.asset_id) in excluded for m in u) for q in pairs]
        cands.append([q for q, x in zip(pairs, out_of_play) if not x])
        rejected_scores.append([q.score for q, x in zip(pairs, out_of_play) if x])

    # 4. Global greedy assignment over plausible pairs, deterministic on ties.
    flat = sorted(((q, i) for i, qs in enumerate(cands) for q in qs
                   if q.score <= MAX_SCORE),
                  key=lambda t: _order(t[0]) + (units[t[1]][0].key,))
    chosen: Dict[int, _Pair] = {}
    used: Set[str] = set()
    for q, i in flat:
        if i in chosen or q.asset_id in used:
            continue
        chosen[i] = q
        used.add(q.asset_id)

    # Scores per asset across units, for the second-best-photo rule.
    asset_scores: Dict[str, List[Tuple[int, int]]] = {}
    for i, qs in enumerate(cands):
        for q in qs:
            asset_scores.setdefault(q.asset_id, []).append((q.score, i))

    # 5-6. Confidence and the pixel rule.
    for i, u in enumerate(units):
        qs = cands[i]
        pick = chosen.get(i)
        if pick is None:
            plausible = [q for q in qs if q.score <= MAX_SCORE]
            alts = tuple((q.asset_id, q.score) for q in plausible[:MAX_ALTERNATIVES])
            conf = LOW if plausible else None
            for m in u:
                out[m.key] = Suggestion(m.key, NO_MATCH, None, None, conf, alts)
            continue
        alts = tuple((q.asset_id, q.score) for q in qs
                     if q.asset_id != pick.asset_id
                     and (q.score <= MAX_SCORE or q.score - pick.score <= AMBIGUITY_MARGIN)
                     )[:MAX_ALTERNATIVES]
        rival = any(q.asset_id != pick.asset_id and q.score - pick.score <= AMBIGUITY_MARGIN
                    for q in qs)
        rival = rival or any(s - pick.score <= AMBIGUITY_MARGIN
                             for s, j in asset_scores[pick.asset_id] if j != i)
        # A rejected frame this close means the proposal may be its twin.
        rival = rival or any(s - pick.score <= AMBIGUITY_MARGIN for s in rejected_scores[i])
        conf = LOW if rival else HIGH
        a_px = pixels_of.get(pick.asset_id)
        for m in u:
            if m.pixels is not None and a_px is not None and a_px <= m.pixels:
                state = ALREADY_ORIGINAL
            else:
                state = PROPOSED
            out[m.key] = Suggestion(m.key, state, pick.asset_id, pick.score, conf, alts)
    return [out[k] for k in sorted(out)]
