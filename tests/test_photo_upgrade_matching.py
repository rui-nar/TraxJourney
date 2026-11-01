"""Matching engine for the album photo upgrade (#33, U5, decision D7)."""
import random
import time
from datetime import date

from src.photo_upgrade import matching as m
from src.photo_upgrade.hashing import Fingerprint

D = date(2024, 5, 10)
NEXT = date(2024, 5, 11)


def fp(base: int, flips: int = 0, w: int = 400, h: int = 300) -> Fingerprint:
    """A fingerprint `flips` bits away from the one `base` names."""
    rng = random.Random(base)
    a, d = rng.getrandbits(64), rng.getrandbits(64)
    for i in range(flips):
        a ^= 1 << i
    return Fingerprint(a, d, w, h)


def trip(key, base=1, flips=0, day=D, pixels=1000, source=None, content_hash=None,
         lat=None, lon=None, w=400, h=300):
    return m.TripPhoto(key, day, lat, lon, fp(base, flips, w, h), pixels, source,
                       content_hash)


def album(aid, base=1, flips=0, day=D, pixels=5000, lat=None, lon=None, w=400, h=300):
    return m.AlbumPhoto(aid, day, lat, lon, fp(base, flips, w, h), pixels)


def by_key(result):
    return {s.key: s for s in result}


def test_an_exact_match_is_proposed_with_high_confidence():
    s = by_key(m.match([trip("p")], [album("a"), album("b", base=2)]))["p"]
    assert (s.state, s.asset_id, s.score, s.confidence) == (m.PROPOSED, "a", 0, m.HIGH)
    assert s.alternatives == ()


def test_a_burst_of_three_frames_is_low_confidence_with_two_alternatives():
    frames = [album("f0"), album("f1", flips=2), album("f2", flips=4)]
    s = by_key(m.match([trip("p")], frames))["p"]
    assert (s.state, s.asset_id, s.confidence) == (m.PROPOSED, "f0", m.LOW)
    assert s.alternatives == (("f1", 2), ("f2", 4))


def test_a_wrong_aspect_ratio_is_excluded_and_two_percent_is_allowed():
    assert by_key(m.match([trip("p")], [album("a", w=400, h=225)]))["p"].state == m.NO_MATCH
    assert by_key(m.match([trip("p")], [album("a", w=405, h=300)]))["p"].asset_id == "a"
    assert by_key(m.match([trip("p")], [album("a", w=420, h=300)]))["p"].state == m.NO_MATCH


def test_an_adjacent_day_asset_wins_only_when_no_same_day_asset_is_as_good():
    p = trip("p")
    assert by_key(m.match([p], [album("n", day=NEXT)]))["p"].asset_id == "n"
    tie = by_key(m.match([p], [album("a_next", day=NEXT), album("z", day=D)]))["p"]
    assert tie.asset_id == "z"           # equal score: same day wins
    better = by_key(m.match([p], [album("n", day=NEXT), album("z", day=D, flips=3)]))["p"]
    assert better.asset_id == "n"        # no adjacent-day penalty
    assert by_key(m.match([p], [album("x", day=date(2024, 5, 13))]))["p"].state == m.NO_MATCH


def test_an_asset_is_never_used_twice_by_different_photos():
    result = m.match([trip("p1"), trip("p2", flips=1)], [album("a")])
    proposed = [s for s in result if s.asset_id == "a" and s.state == m.PROPOSED]
    assert len(proposed) == 1 and proposed[0].key == "p1"


def test_byte_identical_copies_share_an_asset_but_different_photos_compete():
    copies = [trip("m1", content_hash="h", day=D), trip("m2", content_hash="h", day=NEXT)]
    r = by_key(m.match(copies, [album("a")]))
    assert r["m1"].asset_id == r["m2"].asset_id == "a"
    assert r["m1"].state == r["m2"].state == m.PROPOSED

    different = [trip("p1", content_hash="h1"), trip("p2", content_hash="h2")]
    r = by_key(m.match(different, [album("a")]))
    assert r["p1"].state == m.PROPOSED and r["p1"].asset_id == "a"
    assert r["p2"].state == m.NO_MATCH and r["p2"].confidence == m.LOW
    assert ("a", 0) in r["p2"].alternatives


def test_a_photo_without_content_hash_is_never_grouped():
    r = by_key(m.match([trip("p1"), trip("p2")], [album("a")]))   # identical, no hash
    assert [r["p1"].state, r["p2"].state] == [m.PROPOSED, m.NO_MATCH]


def test_od2_sheet_a_score_of_two_is_proposed_and_thirty_three_is_not():
    assert by_key(m.match([trip("p")], [album("a", flips=2)]))["p"].state == m.PROPOSED
    r = by_key(m.match([trip("p")], [album("a", flips=33)]))["p"]
    assert r.state == m.NO_MATCH and r.confidence is None
    assert by_key(m.match([trip("p")], [album("a", flips=12)]))["p"].state == m.PROPOSED
    assert by_key(m.match([trip("p")], [album("a", flips=13)]))["p"].state == m.NO_MATCH


def test_already_original_by_source_removes_the_asset_from_the_pool():
    r = by_key(m.match([trip("p1", source="immich:a"), trip("p2")], [album("a")]))
    assert (r["p1"].state, r["p1"].asset_id) == (m.ALREADY_ORIGINAL, "a")
    assert r["p2"].state == m.NO_MATCH


def test_already_original_when_the_asset_has_no_more_pixels():
    r = by_key(m.match([trip("p", pixels=5000)], [album("a", pixels=5000)]))["p"]
    assert (r.state, r.asset_id) == (m.ALREADY_ORIGINAL, "a")
    r = by_key(m.match([trip("p", pixels=5000)], [album("a", pixels=5001)]))["p"]
    assert r.state == m.PROPOSED


def test_excluded_pairs_are_never_proposed():
    r = by_key(m.match([trip("p")], [album("a"), album("b", flips=2)], {("p", "a")}))["p"]
    assert r.asset_id == "b"
    assert "a" not in [x for x, _ in r.alternatives]
    assert by_key(m.match([trip("p")], [album("a")], {("p", "a")}))["p"].state == m.NO_MATCH


def test_geo_distance_breaks_a_tie_without_filtering():
    p = trip("p", lat=43.6, lon=1.44)
    near, far = album("z_near", lat=43.61, lon=1.45), album("a_far", lat=48.85, lon=2.35)
    assert by_key(m.match([p], [far, near]))["p"].asset_id == "z_near"
    assert by_key(m.match([p], [far]))["p"].asset_id == "a_far"


def test_output_is_deterministic_when_the_input_order_is_shuffled():
    rng = random.Random(7)
    photos = [trip(f"p{i}", base=i % 4, flips=i % 3) for i in range(12)]
    assets = [album(f"a{i}", base=i % 4, flips=i % 5, day=[D, NEXT][i % 2]) for i in range(10)]
    expected = m.match(photos, assets)
    for _ in range(5):
        rng.shuffle(photos)
        rng.shuffle(assets)
        assert m.match(photos, assets) == expected


def test_photos_without_a_fingerprint_are_no_match():
    p = m.TripPhoto("p", D, None, None, None, None, None, None)
    assert by_key(m.match([p], [album("a")]))["p"].state == m.NO_MATCH


def test_rank_candidates_lists_gated_assets_best_first():
    ranked = m.rank_candidates(trip("p"), [
        album("far", flips=40), album("b", flips=2), album("a"),
        album("wide", w=400, h=225), album("late", day=date(2024, 5, 20))])
    assert ranked == [("a", 0), ("b", 2), ("far", 40)]


def test_performance_5000_photos_against_10000_assets():
    rng = random.Random(3)

    def rand_fp():
        return Fingerprint(rng.getrandbits(64), rng.getrandbits(64), 400, 300)

    photos = [m.TripPhoto(f"p{i}", date.fromordinal(D.toordinal() + i % 30), None, None,
                          rand_fp(), 1000, None, None) for i in range(5000)]
    assets = [m.AlbumPhoto(f"a{i}", date.fromordinal(D.toordinal() + i % 30), None, None,
                           rand_fp(), 5000) for i in range(10000)]
    start = time.perf_counter()
    result = m.match(photos, assets)
    assert time.perf_counter() - start < 10
    assert len(result) == 5000
