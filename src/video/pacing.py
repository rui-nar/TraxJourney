"""Clips and their screen time (docs/TRIP_VIDEO_PLAN.md, D4/D5).

Legs are grouped into clips until the clip count fits the length the user
picked, then a :class:`PacingPolicy` shares the clip time between them. Every
trip fits every offered length: a long trip gets fewer, longer-spanning
clips, never a refusal.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Dict, List, Protocol, Sequence, Tuple

from src.video.legs import Leg

TITLE_S = 2.5
END_S = 3.0
FIXED_S = TITLE_S + END_S
MIN_CLIP_S = 1.5
CAP_FRACTION = 0.25

# Flights are long in real time and dull on screen: half weight.
_MODE_WEIGHT: Dict[str, float] = {"flight": 0.5}

# A clip whose legs add up to no real time still needs a weight above zero,
# or no share of the budget could ever reach it and the caps could not add up
# to the total.
_MIN_WEIGHT = 1e-6


def clip_budget_s(total_s: float) -> float:
    """The time left for clips once the title and end cards are paid for."""
    return total_s - FIXED_S


def max_clips(total_s: float) -> int:
    """``N_max``: how many clips of at least :data:`MIN_CLIP_S` fit.

    30 s → 16, 60 s → 36, 90 s → 56.
    """
    return max(0, math.floor(clip_budget_s(total_s) / MIN_CLIP_S + 1e-9))


def mode_weight(mode: str) -> float:
    return _MODE_WEIGHT.get(mode, 1.0)


@dataclass(frozen=True)
class Clip:
    """An ordered run of consecutive legs, shown as one stretch of the video.

    Each leg keeps its own mode, geometry and speed: the icon and badge
    switch as the marker crosses from one to the next.
    """
    legs: Tuple[Leg, ...]

    @property
    def real_s(self) -> float:
        return sum(leg.real_s for leg in self.legs)

    @property
    def mode(self) -> str:
        """The dominant mode: the one with the most real time, the earliest
        on a tie."""
        return _dominant(_mode_seconds(self.legs))

    @property
    def weight(self) -> float:
        return clip_weight(self.real_s, self.mode)


def clip_weight(real_s: float, mode: str) -> float:
    return math.sqrt(max(real_s, 0.0)) * mode_weight(mode)


def _mode_seconds(legs: Sequence[Leg]) -> Dict[str, float]:
    out: Dict[str, float] = {}  # insertion order = first appearance
    for leg in legs:
        out[leg.mode] = out.get(leg.mode, 0.0) + leg.real_s
    return out


def _dominant(seconds: Dict[str, float]) -> str:
    best, best_s = "", -1.0
    for mode, s in seconds.items():
        if s > best_s:
            best, best_s = mode, s
    return best


def merge_same_day(legs: Sequence[Leg]) -> List[List[Leg]]:
    """Runs of consecutive legs with the same mode and date (three city walks
    on one day become one)."""
    groups: List[List[Leg]] = []
    for leg in legs:
        if groups and groups[-1][-1].mode == leg.mode and groups[-1][-1].date == leg.date:
            groups[-1].append(leg)
        else:
            groups.append([leg])
    return groups


def group_clips(legs: Sequence[Leg], n_max: int) -> List[Clip]:
    """*legs* grouped into at most *n_max* clips (at least one).

    Same-mode same-date runs first; then, while there are too many, the
    adjacent pair with the lowest cost merges, cost = ``w_a + w_b``, halved
    when both have the same dominant mode so multi-day stretches of one mode
    merge first. Ties go to the pair further left. O(n log n).
    """
    groups = merge_same_day(legs)
    n = len(groups)
    target = max(1, n_max)
    if n <= target:
        return [Clip(tuple(g)) for g in groups]

    # A doubly-linked list of groups; heap entries carry each side's stamp so
    # an entry made stale by an earlier merge is recognised and dropped.
    members: List[List[Leg]] = [list(g) for g in groups]
    seconds = [_mode_seconds(g) for g in groups]
    real = [sum(s.values()) for s in seconds]
    mode = [_dominant(s) for s in seconds]
    prev = list(range(-1, n - 1))
    nxt = list(range(1, n + 1))
    nxt[-1] = -1
    stamp = [0] * n
    alive = [True] * n

    def cost(a: int, b: int) -> float:
        c = clip_weight(real[a], mode[a]) + clip_weight(real[b], mode[b])
        return c * 0.5 if mode[a] == mode[b] else c

    # Group order never changes, so a group's first leg index orders pairs
    # exactly as their current position would.
    heap: List[Tuple[float, int, int, int, int, int]] = []
    for a in range(n - 1):
        heap.append((cost(a, a + 1), members[a][0].index, a, 0, a + 1, 0))
    heapq.heapify(heap)

    count = n
    while count > target:
        _, _, a, sa, b, sb = heapq.heappop(heap)
        if not (alive[a] and alive[b]) or stamp[a] != sa or stamp[b] != sb or nxt[a] != b:
            continue
        members[a].extend(members[b])
        for m, s in seconds[b].items():
            seconds[a][m] = seconds[a].get(m, 0.0) + s
        real[a] += real[b]
        mode[a] = _dominant(seconds[a])
        stamp[a] += 1
        alive[b] = False
        nxt[a] = nxt[b]
        if nxt[b] != -1:
            prev[nxt[b]] = a
        count -= 1
        if prev[a] != -1:
            p = prev[a]
            heapq.heappush(heap, (cost(p, a), members[p][0].index, p, stamp[p], a, stamp[a]))
        if nxt[a] != -1:
            q = nxt[a]
            heapq.heappush(heap, (cost(a, q), members[a][0].index, a, stamp[a], q, stamp[q]))

    out: List[Clip] = []
    i = 0
    while i != -1:
        out.append(Clip(tuple(members[i])))
        i = nxt[i]
    return out


class PacingPolicy(Protocol):
    def allocate(self, clips: Sequence[Clip], total_s: float) -> List[float]:
        """Each clip's screen time; the times add up to *total_s*, the clip
        budget (the video length less the title and end cards)."""
        ...


@dataclass(frozen=True)
class SqrtBudgetPolicy:
    """D4 option A: clip time ∝ √(real duration) × mode weight, clamped to
    ``[floor_s, max(cap_fraction, 1/n) × total]`` and water-filled.

    The result is ``clamp(λ·wᵢ, floor, cap)`` for the one λ at which the
    times add up to the budget — the fixed point of "clamp the offenders,
    share what is left among the rest, repeat" — so it is monotone in each
    clip's weight.
    """
    floor_s: float = MIN_CLIP_S
    cap_fraction: float = CAP_FRACTION

    def allocate(self, clips: Sequence[Clip], total_s: float) -> List[float]:
        n = len(clips)
        if n == 0:
            return []
        weights = [max(c.weight, _MIN_WEIGHT) for c in clips]
        lo_s = self.floor_s
        hi_s = max(self.cap_fraction, 1.0 / n) * total_s
        if n * lo_s >= total_s:  # more clips than the floor allows: share evenly
            return [total_s / n] * n

        def total_at(lam: float) -> float:
            return sum(min(hi_s, max(lo_s, lam * w)) for w in weights)

        # total_at is non-decreasing in λ; find the linear piece that holds
        # the budget, then solve that piece exactly.
        lo, hi = 0.0, hi_s / min(weights)
        for _ in range(200):
            mid = (lo + hi) / 2
            if total_at(mid) < total_s:
                lo = mid
            else:
                hi = mid
        lam = hi
        pinned = 0.0
        free_w = 0.0
        for w in weights:
            x = lam * w
            if x <= lo_s:
                pinned += lo_s
            elif x >= hi_s:
                pinned += hi_s
            else:
                free_w += w
        if free_w > 0:
            lam = (total_s - pinned) / free_w
        return [min(hi_s, max(lo_s, lam * w)) for w in weights]
