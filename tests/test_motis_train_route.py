"""Tests for MOTIS/Transitous train route resolution (issue #277).

The fixtures under ``tests/fixtures/motis_*.json`` are **real** responses
recorded from https://api.transitous.org (the departure-board entries keep only
the fields the service reads, so the files stay a readable size; nothing else is
altered). No test here touches the network.

The regression this file exists for: a Hamburg Hbf → Offenburg segment on
"ICE 75" could not be resolved. The old code asked a *journey planner* for 15
results from a hardcoded 06:00 and passed a ``lineName`` parameter that route
does not accept — ICE 75 leaves Hamburg at 14:29 with 275 rail departures ahead
of it, so it was unfindable. Matching now happens on a departure board.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest

import src.services.hafas_service as svc
from src.services.hafas_service import HafasError, get_train_route

_FIXTURES = Path(__file__).parent / "fixtures"

# Segment endpoints as a user would place them: the issue's Hamburg Hbf pin and
# Offenburg town centre (~700 m from the station, so trimming has to snap).
_HH_LAT, _HH_LON = 53.5528, 10.0067
_OG_LAT, _OG_LON = 48.4711, 7.9447
_BASEL_LAT, _BASEL_LON = 47.5474, 7.5895   # the trip's real terminus
_DATE = "2026-09-01"


def _fx(name: str):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _km(lat1, lon1, lat2, lon2) -> float:
    dlat = (lat1 - lat2) * 111.0
    dlon = (lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _geocodes() -> dict:
    """Recorded reverse-geocode responses, keyed by the ``place`` MOTIS is sent."""
    return {
        f"{_HH_LAT},{_HH_LON}": _fx("motis_reverse_geocode_hamburg.json"),
        f"{_OG_LAT},{_OG_LON}": _fx("motis_reverse_geocode_offenburg.json"),
    }


class _Motis:
    """Stand-in for the live MOTIS API, recording every request it serves."""

    def __init__(self, geocode=None, boards=None, trip=None, trips=None, statuses=None):
        self.geocode = geocode if geocode is not None else _geocodes()
        self.boards = boards if boards is not None else [_fx("motis_stoptimes_hamburg.json")]
        self.trip = trip if trip is not None else _fx("motis_trip_ice75.json")
        self.trips = trips           # tripId -> payload, when the board is ambiguous
        self.statuses = list(statuses or [])   # queued HTTP statuses to serve first
        self.calls: list[tuple[str, dict]] = []
        self._board_i = 0

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.statuses:
            return _Resp(None, self.statuses.pop(0))
        if url.endswith("/reverse-geocode"):
            if isinstance(self.geocode, dict):
                return _Resp(self.geocode.get(params["place"], []))
            return _Resp(self.geocode)
        if url.endswith("/stoptimes"):
            board = self.boards[min(self._board_i, len(self.boards) - 1)]
            self._board_i += 1
            return _Resp(board)
        if url.endswith("/trip"):
            if self.trips is not None:
                return _Resp(self.trips[params["tripId"]])
            return _Resp(self.trip)
        raise AssertionError(f"unexpected MOTIS request: {url}")

    def paths(self) -> list[str]:
        return [u.rsplit("/", 1)[-1] for u, _ in self.calls]

    def params_for(self, path: str) -> dict:
        return next(p for u, p in self.calls if u.endswith(path))


@pytest.fixture(autouse=True)
def _clear_stop_cache():
    svc._stop_cache.clear()
    yield
    svc._stop_cache.clear()


@pytest.fixture(autouse=True)
def _no_real_backoff():
    """Keep the retry tests instant — the backoff itself is asserted separately."""
    with patch("src.services.hafas_service.time.sleep") as sleep:
        yield sleep


def _resolve(**kwargs):
    args = dict(train_number="ICE 75", date=_DATE,
                start_lat=_HH_LAT, start_lon=_HH_LON,
                end_lat=_OG_LAT, end_lon=_OG_LON)
    args.update(kwargs)
    return get_train_route(**args)


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

class TestIssue277HamburgOffenburg:
    def test_ice75_resolves_with_offenburg_in_the_stop_sequence(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()

        names = [s["name"] for s in route.stops]
        assert names[0] == "Hamburg Hbf"
        assert names[-1] == "Offenburg Bahnhof"
        assert "Frankfurt (Main) Hauptbahnhof" in names
        # Trimmed to the user's own leg — the trip itself runs on to Basel.
        assert "Basel SBB" not in names
        assert len(names) == len(set(names))

    def test_polyline_is_real_track_not_a_two_point_chord(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()

        # Real track, simplified (see _SIMPLIFY_TOLERANCE_M) but nothing like a
        # 2-point chord: hundreds of shape points still describe the line.
        assert 1000 < len(route.polyline) < 4000, "expected simplified real track"
        # [lon, lat] like every other polyline in this codebase.
        lon, lat = route.polyline[0]
        assert 9.5 < lon < 10.5 and 53.0 < lat < 54.0
        assert _km(_HH_LAT, _HH_LON, lat, lon) < 2.0
        lon, lat = route.polyline[-1]
        assert _km(_OG_LAT, _OG_LON, lat, lon) < 2.0

        # …and it follows the line rather than cutting across it: 700+ km of
        # track for a ~530 km crow-flies leg.
        length = sum(
            _km(a[1], a[0], b[1], b[0])
            for a, b in zip(route.polyline, route.polyline[1:])
        )
        assert 600 < length < 900

    def test_three_requests_and_no_journey_planner(self):
        """One reverse-geocode, one board, one trip — and never /journeys."""
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            _resolve()

        assert api.paths() == ["reverse-geocode", "stoptimes", "trip"]
        assert not any("journeys" in url for url, _ in api.calls)
        assert not any("lineName" in params for _, params in api.calls)

    def test_sends_an_identifying_user_agent(self):
        """Transitous' usage policy asks API clients to identify themselves."""
        seen = {}

        def _capture(url, params=None, headers=None, timeout=None):
            seen.update(headers or {})
            return _Motis()(url, params=params, headers=headers, timeout=timeout)

        with patch("src.services.hafas_service.requests.get", _capture):
            _resolve()
        assert seen["User-Agent"].startswith("TraxJourney/")
        assert "(+https://github.com/rui-nar/TraxJourney)" in seen["User-Agent"]


# ---------------------------------------------------------------------------
# The time-window bug: results=15 from a hardcoded 06:00
# ---------------------------------------------------------------------------

class TestServiceDayWindow:
    def test_mid_afternoon_departure_is_found(self):
        """ICE 75 sits 275 entries deep on the real board and must still match.

        This is exactly what ``results=15`` from a hardcoded 06:00 could never
        reach. (The board's first ICE 75 entry is the 12:21 Hamburg Dammtor
        call — the radius sweep sees it — and the trip then trims to the
        14:29 Hamburg Hbf departure the user actually took.)
        """
        board = _fx("motis_stoptimes_hamburg.json")
        index = next(i for i, e in enumerate(board["stopTimes"])
                     if e.get("tripShortName") == "ICE 75")
        assert index > 250, "fixture must reproduce the deep-in-the-day case"
        assert board["stopTimes"][index]["place"]["departure"] == "2026-09-01T12:21:00Z"

        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()
        assert route.stops[-1]["name"] == "Offenburg Bahnhof"

    def test_board_is_requested_from_the_start_of_the_local_service_day(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            _resolve()

        params = api.params_for("/stoptimes")
        # Local midnight at the anchor's own zone (the Hamburg fixture carries
        # tz "Europe/Berlin", CEST in September), not the old hardcoded
        # "{date}T06:00:00+01:00" and not a fixed UTC guess either.
        assert params["time"] == "2026-08-31T22:00:00Z"
        assert params["n"] == 500
        assert params["radius"] == 2000

    @pytest.mark.parametrize("tz,start,end", [
        # CEST (UTC+2) in September.
        ("Europe/Berlin", "2026-08-31T22:00:00+00:00", "2026-09-01T22:00:00+00:00"),
        # EEST (UTC+3).
        ("Europe/Helsinki", "2026-08-31T21:00:00+00:00", "2026-09-01T21:00:00+00:00"),
        # West of UTC. The fixed European window ended at 2026-09-02T00:00Z,
        # which is 19:00 local — every Chicago evening departure tripped the
        # "past the service day" guard and was reported as not running.
        ("America/Chicago", "2026-09-01T05:00:00+00:00", "2026-09-02T05:00:00+00:00"),
        # Half-hour offset, and south of the equator.
        ("Australia/Adelaide", "2026-08-31T14:30:00+00:00", "2026-09-01T14:30:00+00:00"),
    ])
    def test_window_is_the_anchor_local_calendar_day(self, tz, start, end):
        got_start, got_end = svc._service_window(_DATE, tz)
        assert got_start.isoformat() == start
        assert got_end.isoformat() == end

    def test_window_falls_back_when_the_feed_gives_no_zone(self):
        """An unusable tz must not crash the lookup — it degrades to the old
        fixed span, which is still right for the European feeds."""
        for tz in ("", "Not/AZone"):
            start, end = svc._service_window(_DATE, tz)
            assert start.isoformat() == "2026-08-31T21:00:00+00:00"
            assert end.isoformat() == "2026-09-02T00:00:00+00:00"

    def test_stops_paging_once_the_board_runs_past_the_service_day(self):
        """A train that isn't running today must not be chased into tomorrow."""
        board = _fx("motis_stoptimes_hamburg.json")
        board["stopTimes"] = [e for e in board["stopTimes"]
                              if e.get("tripShortName") != "ICE 75"]
        # The real board keeps going into the next service day; a match found
        # there would be the wrong day's train, so the scan must stop first.
        tomorrow = json.loads(json.dumps(board["stopTimes"][-1]))
        tomorrow["place"]["departure"] = "2026-09-02T12:29:00Z"
        tomorrow["tripShortName"] = tomorrow["routeShortName"] = "ICE 75"
        tomorrow["tripId"] = "wrong-day"
        board["stopTimes"].append(tomorrow)

        api = _Motis(boards=[board])
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="not found departing"):
                _resolve()
        assert api.paths().count("stoptimes") == 1

    def test_does_not_match_the_previous_local_days_train(self):
        """The board opens before local midnight, and the scan takes the first
        forward match — so a window that started too early silently returned
        *yesterday*'s trip. Against this fixture a 2026-09-01 request for
        train 11438 used to return the 2026-08-31 trip id.

        "A train number repeats daily" does not rescue this: weekend, seasonal
        and night services may not run on the requested day at all, and quietly
        resolving the wrong day is worse than reporting nothing found.
        """
        board = _fx("motis_stoptimes_hamburg.json")
        yesterday = [e for e in board["stopTimes"]
                     if svc._entry_matches(e, "11438")
                     and e["place"]["departure"] < "2026-08-31T22:00:00Z"]
        assert yesterday, "fixture must contain a pre-midnight 11438"
        assert yesterday[0]["tripId"].startswith("20260831_")

        api = _Motis(boards=[board])
        with patch("src.services.hafas_service.requests.get", api):
            trip_ids = list(svc._iter_trip_ids(
                "x", "2026-09-01", "11438", "Europe/Berlin"))

        assert yesterday[0]["tripId"] not in trip_ids
        assert all(t.startswith("20260901_") for t in trip_ids)

    def test_pages_forward_with_the_cursor_when_the_day_is_not_covered(self):
        board = _fx("motis_stoptimes_hamburg.json")
        head = dict(board, stopTimes=board["stopTimes"][:100],
                    nextPageCursor="LATER|1788300000")
        tail = dict(board, stopTimes=board["stopTimes"][100:])
        api = _Motis(boards=[head, tail])
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()

        assert api.paths().count("stoptimes") == 2
        second = [p for u, p in api.calls if u.endswith("/stoptimes")][1]
        assert second["pageCursor"] == "LATER|1788300000"
        assert "time" not in second
        assert route.stops[-1]["name"] == "Offenburg Bahnhof"


# ---------------------------------------------------------------------------
# precision-7 polyline decoding
# ---------------------------------------------------------------------------

class TestPrecision7Geometry:
    def test_decodes_at_the_precision_the_response_declares(self):
        leg = _fx("motis_trip_ice75.json")["legs"][0]
        assert leg["legGeometry"]["precision"] == 7

        poly = svc._decode_leg_geometry(leg)
        assert len(poly) == leg["legGeometry"]["length"] == 11385

        # Every station on the trip must sit on the decoded line. Decoding a
        # precision-7 polyline at the usual precision 5 puts it ~1000 km out, so
        # this is the assertion that actually pins the precision down.
        places = [leg["from"]] + leg["intermediateStops"] + [leg["to"]]
        for place in places:
            nearest = min(_km(place["lat"], place["lon"], p[1], p[0]) for p in poly)
            assert nearest < 0.1, f"{place['name']} is {nearest:.1f} km off the line"

    def test_decoded_points_are_lon_lat(self):
        leg = _fx("motis_trip_ice75.json")["legs"][0]
        poly = svc._decode_leg_geometry(leg)
        lon, lat = poly[0]
        assert 9.0 < lon < 11.0, "first ordinate must be longitude"
        assert 53.0 < lat < 54.0, "second ordinate must be latitude"

    def test_missing_geometry_decodes_to_nothing(self):
        assert svc._decode_leg_geometry({}) == []
        assert svc._decode_leg_geometry({"legGeometry": {"points": ""}}) == []


# ---------------------------------------------------------------------------
# Trimming the whole trip down to the user's leg
# ---------------------------------------------------------------------------

class TestGeometryTrimming:
    def test_sub_range_does_not_ship_the_whole_trip(self):
        full = svc._decode_leg_geometry(_fx("motis_trip_ice75.json")["legs"][0])
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()

        assert len(route.polyline) < len(full)
        # Nothing south of Offenburg: Basel is 100 km further down the line.
        assert min(p[1] for p in route.polyline) > _BASEL_LAT + 0.5
        assert all(_km(_BASEL_LAT, _BASEL_LON, p[1], p[0]) > 50 for p in route.polyline)

    def test_reversed_segment_reverses_both_stops_and_geometry(self):
        api = _Motis(boards=[_fx("motis_stoptimes_offenburg.json")])
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve(start_lat=_OG_LAT, start_lon=_OG_LON,
                             end_lat=_HH_LAT, end_lon=_HH_LON)

        assert route.stops[0]["name"] == "Offenburg Bahnhof"
        assert route.stops[-1]["name"] == "Hamburg Hbf"
        first_lon, first_lat = route.polyline[0]
        assert _km(_OG_LAT, _OG_LON, first_lat, first_lon) < 2.0
        last_lon, last_lat = route.polyline[-1]
        assert _km(_HH_LAT, _HH_LON, last_lat, last_lon) < 2.0

    def test_trim_polyline_is_a_contiguous_slice(self):
        poly = [[float(i), 0.0] for i in range(10)]
        stops = [{"lat": 0.0, "lon": 2.0}, {"lat": 0.0, "lon": 6.0}]
        assert svc._trim_polyline(poly, stops) == [[float(i), 0.0] for i in range(2, 7)]

    def test_trim_polyline_handles_the_reversed_case(self):
        poly = [[float(i), 0.0] for i in range(10)]
        stops = [{"lat": 0.0, "lon": 6.0}, {"lat": 0.0, "lon": 2.0}]
        assert svc._trim_polyline(poly, stops) == [
            [float(i), 0.0] for i in range(6, 1, -1)]


# ---------------------------------------------------------------------------
# Train-number matching
# ---------------------------------------------------------------------------

class TestNameMatching:
    @pytest.mark.parametrize("candidate,query", [
        ("ICE 75", "ICE 75"),
        ("ICE 75", "ice 75"),
        ("ICE 75", "ICE75"),
        ("ICE 75", "75"),
        ("IC 63", "IC 63"),
        ("RJX 262", "RJX 262"),
        ("000393", "393"),          # Rejseplanen pads trip numbers
        # German boards put the line and the trip number in one string.
        # Normalising it in one pass fused the digits into "811438", so a user
        # typing the line found nothing at all.
        ("RE8 (11438)", "RE8"),
        ("RE8 (11438)", "RE 8"),
        ("RE8 (11438)", "11438"),
        # The Operator dropdown is gone, so the train-number field is the
        # only place left to put the operator - and #277's own title reads
        # "DB ICE 75". The board names only the service.
        ("ICE 75", "DB ICE 75"),
        ("ICE 75", "db ice 75"),
        ("RE8 (11438)", "DB RE8"),
    ])
    def test_matches(self, candidate, query):
        assert svc._name_matches(candidate, query)

    @pytest.mark.parametrize("candidate,query", [
        ("ICE 755", "ICE 75"),      # the substring trap the old matcher fell into
        ("ICE 75", "ICE 755"),
        ("IC 75", "ICE 75"),
        ("ICE 75", ""),
        ("", "ICE 75"),
        # A named query must have its letters confirmed. Waiving that whenever
        # the candidate carried no letters made a French TGV number match a
        # Deutsche Bahn RE8, whose tripShortName is the bare "011438".
        ("011438", "TGV 11438"),
        ("011438", "ICE 11438"),
        ("RE8 (11438)", "TGV 11438"),
        # The operator-prefix allowance runs one way only: the candidate's
        # letters may be a suffix of the query's, never the reverse.
        ("DB ICE 75", "ICE 75"),
        ("ICE 75", "XICE 75"),
        # A leading token is dropped only while what remains still has
        # letters. Without that guard "RS 1" would degrade to a bare "1"
        # and match every service numbered 1.
        ("S 1", "RS 1"),
        ("S1", "RS1"),
    ])
    def test_does_not_match(self, candidate, query):
        assert not svc._name_matches(candidate, query)

    @pytest.mark.parametrize("query,expected", [
        ("RE8", True),          # regression: this found nothing at all
        ("11438", True),
        ("TGV 11438", False),   # regression: this matched the RE8
        ("ICE 11438", False),
        ("IC 11438", False),
        ("DB ICE 75", True),    # regression: this found nothing at all
        ("DB RE8", True),
    ])
    def test_regional_line_on_the_real_board(self, query, expected):
        """Exercised against the recorded Hamburg board, where the entry really
        is routeShortName "RE8 (11438)" / tripShortName "011438"."""
        board = _fx("motis_stoptimes_hamburg.json")
        hit = any(svc._entry_matches(e, query) for e in board["stopTimes"])
        assert hit is expected

    def test_board_entry_matches_on_either_short_name(self):
        assert svc._entry_matches({"routeShortName": "ICE 75", "tripShortName": ""}, "ICE 75")
        assert svc._entry_matches({"routeShortName": "", "tripShortName": "ICE 75"}, "ICE 75")
        # Split across the two fields, as the Danish feed does.
        assert svc._entry_matches(
            {"routeShortName": "ECE", "tripShortName": "000393"}, "ECE 393")

    def test_similar_train_number_is_not_matched_off_the_board(self):
        board = _fx("motis_stoptimes_hamburg.json")
        for entry in board["stopTimes"]:
            if entry.get("tripShortName") == "ICE 75":
                entry["routeShortName"] = entry["tripShortName"] = "ICE 755"
        api = _Motis(boards=[board])
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="not found departing"):
                _resolve()


# ---------------------------------------------------------------------------
# A line number is not a train number (issue #286)
# ---------------------------------------------------------------------------

# RE1 is the Hamburg–Rostock "Hanse-Express". On the recorded board its
# workings split between short runs terminating at Büchen and the long ones
# that carry on to Schwerin or Rostock — and the first one of the service day
# is a Büchen working, so taking the first name match strands a Hamburg →
# Rostock segment. The stop lists below are constructed (/trip was only
# recorded for ICE 75); the trip ids, their order and the headsigns they are
# keyed on all come from the real fixture board.
_RE1_STOPS = [
    ("Hamburg Hbf", 53.5528, 10.0067),
    ("Hamburg-Bergedorf", 53.4894, 10.2033),
    ("Büchen", 53.4757, 10.6206),
    ("Hagenow Land", 53.4256, 11.1836),
    ("Schwerin Hbf", 53.6353, 11.4076),
    ("Bad Kleinen", 53.8129, 11.4818),
    ("Bützow", 53.8478, 11.9930),
    ("Rostock Hbf", 54.0783, 12.1311),
]
_RE1_TERMINUS = {"Büchen": 2, "Schwerin Hauptbahnhof": 4, "Rostock Hauptbahnhof": 7}

# The user's pins: Rostock town centre (~1 km off the station) and Büchen.
_ROSTOCK_LAT, _ROSTOCK_LON = 54.0887, 12.1405
_BUECHEN_LAT, _BUECHEN_LON = 53.4757, 10.6206
_ROME_LAT, _ROME_LON = 41.9028, 12.4964


def _re1_entries(board=None) -> list[dict]:
    """The RE1 board entries inside the 2026-09-01 service day, in board order."""
    board = board or _fx("motis_stoptimes_hamburg.json")
    return [e for e in board["stopTimes"]
            if (e.get("routeShortName") or "").startswith("RE1 (")
            and e["place"]["departure"] >= "2026-08-31T22:00:00Z"]


def _trip_payload(stops) -> dict:
    """A /trip response carrying only a stop sequence — no geometry."""
    places = [{"name": n, "lat": lat, "lon": lon} for n, lat, lon in stops]
    return {"legs": [{"from": places[0], "to": places[-1],
                      "intermediateStops": places[1:-1]}]}


def _re1_trips() -> dict:
    return {e["tripId"]: _trip_payload(_RE1_STOPS[:_RE1_TERMINUS[e["headsign"]] + 1])
            for e in _re1_entries()}


class TestAmbiguousLineNumber:
    def test_scan_continues_past_a_working_that_misses_the_destination(self):
        entries = _re1_entries()
        assert entries[0]["headsign"] == "Büchen", "fixture must open with a short working"
        assert entries[1]["headsign"] == "Rostock Hauptbahnhof"

        api = _Motis(trips=_re1_trips())
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve(train_number="RE1",
                             end_lat=_ROSTOCK_LAT, end_lon=_ROSTOCK_LON)

        assert route.stops[0]["name"] == "Hamburg Hbf"
        assert route.stops[-1]["name"] == "Rostock Hbf"
        # The Büchen working was tried and rejected, the next one taken.
        assert api.paths().count("trip") == 2
        tried = [p["tripId"] for u, p in api.calls if u.endswith("/trip")]
        assert tried == [entries[0]["tripId"], entries[1]["tripId"]]

    def test_the_first_working_still_wins_when_it_fits(self):
        """The fallback must not walk past a candidate that does serve the
        segment: a Hamburg → Büchen leg is exactly what the first one is."""
        api = _Motis(trips=_re1_trips())
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve(train_number="RE1",
                             end_lat=_BUECHEN_LAT, end_lon=_BUECHEN_LON)

        assert route.stops[-1]["name"] == "Büchen"
        assert api.paths().count("trip") == 1

    def test_an_unambiguous_number_still_costs_one_trip_request(self):
        """The guard against the fallback multiplying requests: ICE 75 is on
        the board once, so nothing about its resolve may change."""
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()

        assert route.stops[-1]["name"] == "Offenburg Bahnhof"
        # One board page, one trip: the scan stops pulling the moment a
        # candidate resolves, so it never reads past the match it used.
        assert api.paths() == ["reverse-geocode", "stoptimes", "trip"]

        board = _Motis()
        with patch("src.services.hafas_service.requests.get", board):
            assert len(list(svc._iter_trip_ids(
                "x", _DATE, "ICE 75", "Europe/Berlin"))) == 1

    def test_all_candidates_rejected_is_not_reported_as_not_found(self):
        api = _Motis(trips=_re1_trips())
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError) as raised:
                _resolve(train_number="RE1", end_lat=_ROME_LAT, end_lon=_ROME_LON)

        message = str(raised.value)
        assert "not found departing" not in message
        assert "none of the" in message and "RE1" in message
        # …and the bound held: one /trip per candidate, no more.
        assert api.paths().count("trip") == svc._MAX_TRIP_CANDIDATES

    def test_a_single_candidate_keeps_its_own_failure_message(self):
        """ICE 75 is unambiguous, so the user must still be told the train does
        not serve the endpoint rather than given the many-workings message."""
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="does not stop near the end"):
                _resolve(end_lat=_ROME_LAT, end_lon=_ROME_LON)
        assert api.paths().count("trip") == 1

    def test_candidate_scan_is_bounded(self):
        """Each candidate is a /trip request against a shared free service, so
        a line running all day must not turn one resolve into 20 calls."""
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            ids = list(svc._iter_trip_ids("x", _DATE, "RE1", "Europe/Berlin"))

        assert len(_re1_entries()) > svc._MAX_TRIP_CANDIDATES
        assert ids == [e["tripId"] for e in _re1_entries()][:svc._MAX_TRIP_CANDIDATES]

    def test_one_working_seen_at_two_stops_counts_once(self):
        """The radius sweep lists a trip once per stop it calls at, so without
        deduplication the whole budget can go on a single train."""
        board = _fx("motis_stoptimes_hamburg.json")
        first = _re1_entries(board)[0]
        twin = json.loads(json.dumps(first))
        twin["place"]["name"] = "Hamburg Dammtor"
        board["stopTimes"].insert(board["stopTimes"].index(first) + 1, twin)

        api = _Motis(boards=[board])
        with patch("src.services.hafas_service.requests.get", api):
            ids = list(svc._iter_trip_ids("x", _DATE, "RE1", "Europe/Berlin"))

        assert len(ids) == len(set(ids)) == svc._MAX_TRIP_CANDIDATES

    def test_a_failing_trip_request_ends_the_lookup(self):
        """A rejected candidate means "try the next one"; an unreachable /trip
        does not — retrying it down the whole board would hammer a service that
        is already failing and report the wrong reason."""
        api = _Motis(trips=_re1_trips())
        with patch("src.services.hafas_service.requests.get", api), \
             patch("src.services.hafas_service._trip_stops_and_geometry",
                   side_effect=HafasError("MOTIS /trip unavailable after 3 attempts")):
            with pytest.raises(HafasError, match="unavailable after 3 attempts"):
                _resolve(train_number="RE1",
                         end_lat=_ROSTOCK_LAT, end_lon=_ROSTOCK_LON)


# ---------------------------------------------------------------------------
# Country coverage — one index instead of four provider backends
# ---------------------------------------------------------------------------

class TestFinlandViaTheSameIndex:
    """Finland used to need its own rata.digitraffic.fi wire format."""

    def test_suburban_anchor_plus_radius_finds_the_long_distance_train(self):
        # Helsinki's best-scoring stop is the tram/commuter "Päärautatieasema";
        # the radius sweep is what brings the IC departures onto its board.
        api = _Motis(
            geocode=_fx("motis_reverse_geocode_helsinki.json"),
            boards=[_fx("motis_stoptimes_helsinki.json")],
        )
        with patch("src.services.hafas_service.requests.get", api):
            trip_ids = list(svc._iter_trip_ids(
                svc._nearest_stop(60.172097, 24.941249)["id"], _DATE, "IC 63"))

        assert trip_ids
        anchor = api.params_for("/reverse-geocode")
        assert anchor["type"] == "STOP"
        assert api.params_for("/stoptimes")["radius"] == 2000


class TestAnchorSelection:
    def test_anchor_takes_the_best_scoring_stop_whatever_its_modes(self):
        """MOTIS returns only five reverse-geocode candidates and ignores a count
        parameter, so a town-centre pin can come back as five bus stops with the
        station nowhere in the list. Finding the station is the radius sweep's
        job, not a mode filter on the anchor — filtering here found nothing at
        all for Offenburg."""
        api = _Motis(boards=[_fx("motis_stoptimes_offenburg.json")])
        with patch("src.services.hafas_service.requests.get", api):
            anchor = svc._nearest_stop(_OG_LAT, _OG_LON)
            route = _resolve(start_lat=_OG_LAT, start_lon=_OG_LON,
                             end_lat=_HH_LAT, end_lon=_HH_LON)

        assert anchor["name"] == "Offenburg, Stadtkirche"
        assert not set(_fx("motis_reverse_geocode_offenburg.json")[0]["modes"]) & {
            "HIGHSPEED_RAIL", "LONG_DISTANCE", "REGIONAL_RAIL"}
        assert route.stops[0]["name"] == "Offenburg Bahnhof"

    def test_anchor_ignores_a_candidate_far_from_the_pin(self):
        api = _Motis(geocode=[{"id": "far", "name": "Far away", "lat": 0.0,
                               "lon": 0.0, "modes": ["LONG_DISTANCE"]}])
        with patch("src.services.hafas_service.requests.get", api):
            assert svc._nearest_stop(_HH_LAT, _HH_LON) is None

    def test_no_station_nearby_raises(self):
        api = _Motis(geocode=[])
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="Could not locate a station"):
                _resolve()


# ---------------------------------------------------------------------------
# Failure handling — the contract callers depend on
# ---------------------------------------------------------------------------

class TestRetryAndErrors:
    def test_retries_a_transient_503_then_succeeds(self, _no_real_backoff):
        api = _Motis(statuses=[503, 503])
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()

        assert route.stops[-1]["name"] == "Offenburg Bahnhof"
        # Two failed reverse-geocode attempts, then the real three calls.
        assert api.paths() == ["reverse-geocode"] * 3 + ["stoptimes", "trip"]
        assert [c.args[0] for c in _no_real_backoff.call_args_list] == [1.0, 2.0]

    def test_exhausted_retries_raise_the_error_callers_catch(self):
        api = _Motis(statuses=[503, 503, 503])
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="unavailable after 3 attempts"):
                _resolve()
        assert api.paths() == ["reverse-geocode"] * 3

    def test_rate_limiting_is_retried_too(self):
        api = _Motis(statuses=[429])
        with patch("src.services.hafas_service.requests.get", api):
            assert _resolve().stops[-1]["name"] == "Offenburg Bahnhof"

    def test_a_4xx_is_not_retried(self):
        api = _Motis(statuses=[404])
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="Train route lookup failed"):
                _resolve()
        assert api.paths() == ["reverse-geocode"]

    def test_non_json_body_raises_hafas_error(self):
        """The Rejseplanen trap: an HTTP 299 HTML page that .json() chokes on."""
        class _Html(_Resp):
            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        with patch("src.services.hafas_service.requests.get",
                   lambda *a, **k: _Html(None, 299)):
            with pytest.raises(HafasError, match="Train route lookup failed"):
                _resolve()

    def test_train_that_does_not_serve_the_destination_is_rejected(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="does not stop near the end"):
                _resolve(end_lat=41.9028, end_lon=12.4964)   # Rome

    def test_train_that_does_not_serve_the_start_is_rejected(self):
        """The end guard had a test; the start guard did not. A trip whose
        nearest stop to the *start* pin is far away must be rejected too —
        _trim_stops would otherwise happily snap to whatever is closest."""
        board = _fx("motis_stoptimes_hamburg.json")
        # Anchor near Hamburg so the board is found, but ask for a start pin
        # the matched trip never goes near.
        api = _Motis(boards=[board])
        with patch("src.services.hafas_service.requests.get", api),              patch("src.services.hafas_service._nearest_stop",
                   return_value={"id": "x", "name": "Hamburg Hbf",
                                 "tz": "Europe/Berlin"}):
            with pytest.raises(HafasError, match="does not stop near the start"):
                _resolve(start_lat=41.9028, start_lon=12.4964)   # Rome

    def test_missing_date_raises_without_calling_the_api(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            with pytest.raises(HafasError, match="No usable service date"):
                _resolve(date="")
        assert api.calls == []

    def test_outbound_calls_are_counted(self, metric):
        before = metric("traxjourney_external_requests_total",
                        service="hafas", endpoint="motis/trip", outcome="success")
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            _resolve()
        after = metric("traxjourney_external_requests_total",
                       service="hafas", endpoint="motis/trip", outcome="success")
        assert after - before == 1


class TestReverseGeocodeCache:
    def test_same_endpoint_is_geocoded_once(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            _resolve()
            _resolve()
        assert api.paths().count("reverse-geocode") == 1


class TestGeometrySimplification:
    """The polyline is persisted, re-served by /geo on every load, and turned
    into one draggable marker per vertex by the track editor (which has no cap
    of its own), so raw MOTIS shape density is not free."""

    def test_geometry_is_simplified_before_it_leaves_the_service(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            route = _resolve()

        raw = svc._trim_polyline(
            svc._decode_leg_geometry(_fx("motis_trip_ice75.json")["legs"][0]),
            route.stops)
        assert len(raw) > 9000, "the untrimmed source really is that dense"
        assert len(route.polyline) < len(raw) / 4
        assert len(json.dumps(route.polyline)) < 80_000

    def test_every_dropped_point_stays_within_the_tolerance(self):
        """RDP's contract, checked rather than assumed: no original point is
        further than the tolerance from the line that replaces it."""
        poly = svc._decode_leg_geometry(_fx("motis_trip_ice75.json")["legs"][0])
        simple = svc._simplify(poly)
        assert 2 <= len(simple) < len(poly)

        # _simplify returns the surviving point objects from *poly*, so identity
        # recovers exactly which indices it kept — no coordinate matching, which
        # is what made an earlier version of this check walk the wrong segment.
        by_id = {id(pt): i for i, pt in enumerate(poly)}
        kept = [by_id[id(pt)] for pt in simple]
        assert kept == sorted(kept) and kept[0] == 0 and kept[-1] == len(poly) - 1

        worst = 0.0
        for start, end in zip(kept, kept[1:]):
            a, b = poly[start], poly[end]
            for k in range(start + 1, end):
                worst = max(worst, _perp_km(poly[k], a, b))
        # The projection uses one cos(latitude) scale for the whole line, so
        # over a 6-degree north-south route the effective tolerance drifts by a
        # few percent (measured: 2.07 m for a nominal 2 m). Allow that margin
        # rather than pretend the constant is exact.
        assert worst * 1000 <= svc._SIMPLIFY_TOLERANCE_M * 1.10, f"{worst * 1000:.2f} m"

    def test_a_short_line_is_left_alone(self):
        assert svc._simplify([[0.0, 0.0], [1.0, 1.0]]) == [[0.0, 0.0], [1.0, 1.0]]

    def test_endpoints_are_never_dropped(self):
        poly = [[i * 1e-4, 0.0] for i in range(500)]
        simple = svc._simplify(poly)
        assert simple[0] == poly[0] and simple[-1] == poly[-1]


class TestAnchorCache:
    def test_a_failed_lookup_is_not_cached(self):
        """The cache used to store None, so one transient reverse-geocode
        failure pinned "no station here" for the worker's lifetime."""
        empty = _Motis(geocode=[])
        with patch("src.services.hafas_service.requests.get", empty):
            assert svc._nearest_stop(_HH_LAT, _HH_LON) is None

        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            assert svc._nearest_stop(_HH_LAT, _HH_LON)["name"] == "Hamburg Hauptbahnhof"

    def test_the_cache_is_bounded(self):
        api = _Motis()
        with patch("src.services.hafas_service.requests.get", api):
            for i in range(svc._STOP_CACHE_MAX + 5):
                svc._nearest_stop(_HH_LAT + i * 0.01, _HH_LON)
        assert len(svc._stop_cache) <= svc._STOP_CACHE_MAX


def _perp_km(p, a, b):
    """Perpendicular distance from *p* to segment a-b, in km (equirectangular)."""
    scale = math.cos(math.radians(a[1]))
    px, py = (p[0] - a[0]) * scale * 111.0, (p[1] - a[1]) * 111.0
    bx, by = (b[0] - a[0]) * scale * 111.0, (b[1] - a[1]) * 111.0
    den = math.hypot(bx, by)
    return math.hypot(px, py) if den == 0 else abs(bx * py - by * px) / den
