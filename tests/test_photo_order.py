"""Tests for the rank placement helpers in ``api/photo_order.py`` (issue #237)
and the content hashes they carry (issue #566).

Pure functions, so every test also checks its inputs come back unchanged.
"""
from __future__ import annotations

import copy
import itertools
import json

import pytest

from api.photo_order import clear, dump_state, has_hash, load_state, place, remove, replace

_H1, _H2, _H3 = ("a" * 64, "0123456789abcdef" * 4, "f" * 64)


def _state(ranks=None, epoch=0, hashes=None):
    return {"epoch": epoch, "ranks": dict(ranks or {}), "hashes": dict(hashes or {})}


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
    assert load_state(raw) == {"epoch": 0, "ranks": {}, "hashes": {}}


def test_load_state_keeps_the_epoch_when_ranks_are_unreadable():
    assert load_state('{"epoch": 4, "ranks": "junk"}') == {"epoch": 4, "ranks": {}, "hashes": {}}


def test_load_state_drops_malformed_rank_entries_only():
    raw = json.dumps({"epoch": 1, "ranks": {"a": 2, "b": "3", "c": None, "d": True, "e": 0}})
    assert load_state(raw) == {"epoch": 1, "ranks": {"a": 2, "e": 0}, "hashes": {}}


def test_dump_then_load_round_trips():
    state = _state({"a": 0, "b": 3}, epoch=5, hashes={"a": _H1, "c": _H2})
    before = copy.deepcopy(state)
    assert load_state(dump_state(state)) == state
    assert state == before


def test_dump_writes_all_three_keys():
    assert json.loads(dump_state(_state())) == {"epoch": 0, "ranks": {}, "hashes": {}}


def test_a_state_written_before_hashes_existed_reads_as_no_hashes():
    assert load_state('{"epoch": 2, "ranks": {"a": 0}}') == {"epoch": 2, "ranks": {"a": 0}, "hashes": {}}


@pytest.mark.parametrize("hashes", ['"junk"', "[]", "null", "42", '{"a": 1}'])
def test_malformed_hashes_never_reset_the_epoch_or_ranks(hashes):
    raw = '{"epoch": 6, "ranks": {"a": 1}, "hashes": %s}' % hashes
    assert load_state(raw) == {"epoch": 6, "ranks": {"a": 1}, "hashes": {}}


def test_load_state_drops_malformed_hash_entries_only():
    raw = json.dumps({"epoch": 1, "ranks": {}, "hashes": {
        "ok": _H1,
        "upper": _H1.upper(),
        "short": _H1[:63],
        "long": _H1 + "a",
        "newline": _H1 + "\n",
        "not-hex": "g" * 64,
        "int": 7,
        "none": None,
        "ok2": _H2,
    }})
    assert load_state(raw)["hashes"] == {"ok": _H1, "ok2": _H2}


def test_clear_bumps_the_epoch_and_empties_ranks():
    state = _state({"a": 0, "b": 1}, epoch=2)
    before = copy.deepcopy(state)
    assert clear(state) == {"epoch": 3, "ranks": {}, "hashes": {}}
    assert state == before
    assert clear(load_state(None)) == {"epoch": 1, "ranks": {}, "hashes": {}}


# ── Content hashes (issue #566) ─────────────────────────────────────────────

def test_place_with_a_hash_records_it():
    state = _state({"a": 0}, epoch=4, hashes={"a": _H1})
    before = copy.deepcopy(state)
    photos, state2 = place(["a"], state, "b", 1, content_hash=_H2)
    assert state == before
    assert photos == ["a", "b"]
    assert state2 == _state({"a": 0, "b": 1}, epoch=4, hashes={"a": _H1, "b": _H2})


def test_place_of_an_unranked_photo_records_its_hash_too():
    _, state = place([], _state(), "m", None, content_hash=_H1)
    assert state["hashes"] == {"m": _H1}


def test_place_without_a_hash_keeps_the_others_and_drops_a_stale_one_for_it():
    _, state = place(["a"], _state(hashes={"a": _H1, "u": _H2}), "u", None)
    assert state["hashes"] == {"a": _H1}


def test_remove_drops_the_hash():
    photos, state = ["a", "b"], _state({"a": 0, "b": 1}, epoch=2, hashes={"a": _H1, "b": _H2})
    before = (copy.deepcopy(photos), copy.deepcopy(state))
    photos2, state2 = remove(photos, state, "a")
    assert (photos, state) == before
    assert photos2 == ["b"]
    assert state2 == _state({"b": 1}, epoch=2, hashes={"b": _H2})


def test_replace_carries_the_hash_to_the_new_photo():
    photos, state = ["a", "b"], _state({"a": 0}, hashes={"a": _H1, "b": _H2})
    before = (copy.deepcopy(photos), copy.deepcopy(state))
    photos2, state2 = replace(photos, state, "a", "new")
    assert (photos, state) == before
    assert photos2 == ["new", "b"]
    assert state2["hashes"] == {"new": _H1, "b": _H2}
    # The replaced download still counts as present; the original does not
    # come back on a re-import.
    assert has_hash(state2, photos2, _H1)


def test_replace_of_an_unhashed_photo_leaves_the_new_one_unhashed():
    _, state = replace(["m"], _state(hashes={"new": _H3}), "m", "new")
    assert state["hashes"] == {}


def test_clear_empties_the_hashes():
    assert clear(_state(hashes={"a": _H1}, epoch=1))["hashes"] == {}


def test_has_hash_only_counts_photos_in_the_list():
    state = _state(hashes={"a": _H1, "gone": _H2})
    before = copy.deepcopy(state)
    assert has_hash(state, ["a", "b"], _H1)
    assert not has_hash(state, ["a", "b"], _H2)  # stale entry for a removed UUID
    assert not has_hash(state, ["a", "b"], _H3)
    assert not has_hash(_state(), [], _H1)
    assert state == before


def test_has_hash_never_matches_a_missing_hash():
    # "m" is a manual upload: it records no hash, so hashes.get("m") is None.
    state = _state(hashes={"a": _H1})
    assert not has_hash(state, ["a", "m"], None)


@pytest.mark.parametrize("bad", ["", "junk", _H1.upper(), _H1[:63], _H1 + "\n"])
def test_has_hash_never_matches_a_malformed_hash(bad):
    # Even a malformed entry already stored in the state does not match.
    assert not has_hash(_state(hashes={"a": _H1, "m": bad}), ["a", "m"], bad)
