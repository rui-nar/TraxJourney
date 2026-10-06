"""Tests for the rank placement helpers in ``api/photo_order.py`` (issue #237).

Pure functions, so every test also checks its inputs come back unchanged.
"""
from __future__ import annotations

import copy
import itertools
import json

import pytest

from api.photo_order import clear, dump_state, load_state, place, remove, replace


def _state(ranks=None, epoch=0):
    return {"epoch": epoch, "ranks": dict(ranks or {})}


def _place_all(arrivals, photos=None, state=None):
    """Feed ``(uuid, rank)`` arrivals through ``place``, checking inputs stay untouched."""
    photos = list(photos or [])
    state = state or _state()
    for uuid, rank in arrivals:
        before = (copy.deepcopy(photos), copy.deepcopy(state))
        new_photos, new_state = place(photos, state, uuid, rank)
        assert (photos, state) == before
        photos, state = new_photos, new_state
    return photos, state


@pytest.mark.parametrize("arrival", list(itertools.permutations(range(5))))
def test_every_arrival_order_ends_in_rank_order(arrival):
    photos, state = _place_all([(f"p{r}", r) for r in arrival])
    assert photos == ["p0", "p1", "p2", "p3", "p4"]
    assert state["ranks"] == {f"p{r}": r for r in range(5)}


@pytest.mark.parametrize("manual_after", range(6))
def test_a_manual_append_during_an_import_ends_after_every_ranked_photo(manual_after):
    arrivals = [(f"p{r}", r) for r in (3, 0, 4, 1, 2)]
    arrivals.insert(manual_after, ("manual", None))
    photos, state = _place_all(arrivals)
    assert photos == ["p0", "p1", "p2", "p3", "p4", "manual"]
    assert "manual" not in state["ranks"]


def test_equal_ranks_keep_arrival_order_and_both_survive():
    photos, state = _place_all([("b", 1), ("first1", 1), ("a", 0), ("second1", 1), ("c", 2)])
    assert photos == ["a", "b", "first1", "second1", "c"]
    assert state["ranks"] == {"a": 0, "b": 1, "first1": 1, "second1": 1, "c": 2}


def test_remove_then_late_arrival_places_correctly():
    # The positional design shifted later slots after a delete; ranks don't.
    photos, state = _place_all([("p0", 0), ("p1", 1), ("p3", 3)])
    before = (copy.deepcopy(photos), copy.deepcopy(state))
    photos2, state2 = remove(photos, state, "p1")
    assert (photos, state) == before
    assert photos2 == ["p0", "p3"]
    assert state2["ranks"] == {"p0": 0, "p3": 3}
    photos3, _ = _place_all([("p2", 2), ("p4", 4)], photos2, state2)
    assert photos3 == ["p0", "p2", "p3", "p4"]


def test_remove_of_an_absent_uuid_changes_nothing():
    photos, state = ["a", "b"], _state({"a": 0}, epoch=2)
    assert remove(photos, state, "zz") == (["a", "b"], _state({"a": 0}, epoch=2))


def test_replace_keeps_position_and_rank():
    photos, state = _place_all([("p0", 0), ("p1", 1), ("p2", 2), ("m", None)])
    before = (copy.deepcopy(photos), copy.deepcopy(state))
    photos2, state2 = replace(photos, state, "p1", "new")
    assert (photos, state) == before
    assert photos2 == ["p0", "new", "p2", "m"]
    assert state2["ranks"] == {"p0": 0, "new": 1, "p2": 2}
    # A later arrival still lands by rank around the replacement.
    photos3, _ = _place_all([("late1", 1)], photos2, state2)
    assert photos3 == ["p0", "new", "late1", "p2", "m"]


def test_replace_of_an_unranked_photo_leaves_the_new_one_unranked():
    photos, state = _place_all([("p0", 0), ("m", None)])
    photos2, state2 = replace(photos, state, "m", "m2")
    assert photos2 == ["p0", "m2"]
    assert state2["ranks"] == {"p0": 0}


def test_replace_of_an_absent_old_returns_none_and_changes_nothing():
    photos, state = ["a", "b"], _state({"a": 0, "b": 1}, epoch=3)
    before = (copy.deepcopy(photos), copy.deepcopy(state))
    assert replace(photos, state, "gone", "new") is None
    assert (photos, state) == before


def test_stale_rank_entries_for_absent_uuids_are_ignored():
    # Archive import rewrote photos_json without touching ranks.
    photos = ["x", "y"]
    state = _state({"ghost0": 0, "ghost9": 9})
    photos2, state2 = place(photos, state, "p5", 5)
    assert photos2 == ["p5", "x", "y"]  # x, y unranked; ghosts don't count
    photos3, _ = place(["p5", "x"], _state({"p5": 5, "ghost2": 2}), "p3", 3)
    assert photos3 == ["p3", "p5", "x"]


def test_placing_an_unranked_uuid_drops_a_stale_rank_for_it():
    photos, state = place([], _state({"u": 4}), "u", None)
    assert photos == ["u"]
    assert state["ranks"] == {}


def test_placing_an_already_present_uuid_is_refused():
    with pytest.raises(ValueError):
        place(["a"], _state(), "a", 0)


def test_place_keeps_the_epoch():
    _, state = place([], _state(epoch=7), "a", 0)
    assert state["epoch"] == 7


@pytest.mark.parametrize("raw", [None, "", "{}", "not json", "[1, 2]", "42", "null",
                                 '{"epoch": "x", "ranks": []}'])
def test_load_state_defaults_on_missing_or_unreadable(raw):
    assert load_state(raw) == {"epoch": 0, "ranks": {}}


def test_load_state_keeps_the_epoch_when_ranks_are_unreadable():
    assert load_state('{"epoch": 4, "ranks": "junk"}') == {"epoch": 4, "ranks": {}}


def test_load_state_drops_malformed_rank_entries_only():
    raw = json.dumps({"epoch": 1, "ranks": {"a": 2, "b": "3", "c": None, "d": True, "e": 0}})
    assert load_state(raw) == {"epoch": 1, "ranks": {"a": 2, "e": 0}}


def test_dump_then_load_round_trips():
    state = _state({"a": 0, "b": 3}, epoch=5)
    before = copy.deepcopy(state)
    assert load_state(dump_state(state)) == state
    assert state == before


def test_clear_bumps_the_epoch_and_empties_ranks():
    state = _state({"a": 0, "b": 1}, epoch=2)
    before = copy.deepcopy(state)
    assert clear(state) == {"epoch": 3, "ranks": {}}
    assert state == before
    assert clear(load_state(None)) == {"epoch": 1, "ranks": {}}
