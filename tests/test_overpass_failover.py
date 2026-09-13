"""Tests for _overpass transport: how it paces, waits and fails over.

Regression cover for the 2026-09-06 incident. Overpass-api.de was healthy the
whole time — it answered a full 5.2 MB strategy-C query in 8.4s with both slots
free — but every rail resolve came back degraded after 213-316s, against a
client that gives up at 120s. The transport treated a 429 ("busy, come back in
a moment") exactly like a dead host and immediately failed over to two mirrors
that cost the full 45s socket timeout each, roughly 90s per query. It then
reported only the *last* error, so the logs named a mirror and never revealed
that the primary had been reached at all.
"""

from contextlib import contextmanager

import pytest

from src.services import overpass_service as ov
from src.services.overpass_service import OverpassError, _overpass

_PRIMARY = ov._OVERPASS_ENDPOINTS[0]
_FALLBACK = ov._OVERPASS_ENDPOINTS[-1]
_QUERY = "[out:json];node(1);out;"


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self.text = text
        self.content = (text or "").encode()

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


@pytest.fixture
def transport(monkeypatch):
    """Record every POST and every sleep; drive responses from a script.

    Both are the measurements under test: which hosts we talked to, and how
    long we were willing to wait rather than talk to a different one.
    """
    calls: list[str] = []
    sleeps: list[float] = []
    script: dict[str, list] = {}

    def _post(url, **_kwargs):
        calls.append(url)
        queued = script.get(url)
        if not queued:
            raise AssertionError(f"unscripted call to {url}")
        outcome = queued.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ov.requests, "post", _post)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    # No broker in the test suite, so the limiter is already a no-op; pin it so
    # a developer machine with REDIS_URL set doesn't change what these assert.
    monkeypatch.setattr(ov, "get_redis", lambda: None, raising=False)
    monkeypatch.setattr("src.jobs.upstream_slots.get_redis", lambda: None)
    # Cooldowns are deliberately process-wide state, so one test marking a host
    # would otherwise make every later test skip it.
    monkeypatch.setattr("src.jobs.upstream_slots._local_cooldowns", {})

    return type("T", (), {
        "calls": calls, "sleeps": sleeps, "script": script,
    })()


class TestBackOffRatherThanRetry:
    """overpass-api.de asks for a 30s pause after a 429, and its operators ban
    clients that repeatedly trigger one — a high 429 *rate* being a trigger by
    itself. An earlier version of this module waited ~5s and retried the same
    host three times; this deployment's IPv4 address was blocked within minutes
    of shipping it. Nothing may retry in band.
    """

    def test_a_rate_limited_host_is_not_retried(self, transport):
        transport.script[_PRIMARY] = [_Resp(429)]
        transport.script[_FALLBACK] = [_Resp(200, payload={"elements": ["fb"]})]

        assert _overpass(_QUERY) == {"elements": ["fb"]}
        assert transport.calls.count(_PRIMARY) == 1, "one attempt per host, never more"
        assert transport.sleeps == [], "waiting in-band is what got us banned"

    def test_a_rate_limited_host_is_skipped_by_later_queries(self, transport):
        """The unit of back-off is the host, not the request. A resolve makes
        several queries, so pausing one while the next hits the same host would
        honour the letter and miss the point."""
        transport.script[_PRIMARY] = [_Resp(429)]
        transport.script[_FALLBACK] = [_Resp(200, payload={}), _Resp(200, payload={})]

        _overpass("query one")
        _overpass("query two")
        assert transport.calls.count(_PRIMARY) == 1, "the second query must skip it"

    def test_the_cooldown_meets_the_documented_minimum(self):
        """The policy is 'pause for 30 seconds'; this is that with margin."""
        assert ov._COOLDOWN_RATE_LIMITED_S >= 30

    @pytest.mark.parametrize("status", sorted(ov._BACK_OFF_STATUSES))
    def test_every_back_off_status_cools_the_host(self, transport, status):
        """504 included: the server documents it as 'too busy to handle your
        request' — resource admission refused, not a generic gateway error."""
        transport.script[_PRIMARY] = [_Resp(status)]
        transport.script[_FALLBACK] = [_Resp(200, payload={})]

        _overpass(_QUERY)
        assert ov.is_cooling(ov._slot_name(_PRIMARY))

    def test_an_unreachable_host_backs_off_far_harder(self, transport):
        """A refused connection is how a block presents. Reconnecting tells us
        nothing it has not already said, and may prolong it."""
        transport.script[_PRIMARY] = [OSError("connection refused")]
        transport.script[_FALLBACK] = [_Resp(200, payload={})]

        _overpass(_QUERY)
        assert ov._COOLDOWN_UNREACHABLE_S >= 10 * ov._COOLDOWN_RATE_LIMITED_S

    def test_no_dead_parsing_of_headers_overpass_never_sends(self):
        """Overpass sends no Retry-After, and its 429 body carries no 'in N
        seconds' — that phrasing exists only in /api/status. Both parse paths
        were inert, and their 5s fallback was what actually ran, every time."""
        assert not hasattr(ov, "_retry_after_seconds")
        assert not hasattr(ov, "_SLOT_HINT_RE")


class TestClientPatience:
    def test_the_socket_timeout_outlasts_the_admission_window(self):
        """The server enqueues for up to 15s deciding whether to admit a query,
        then runs it for up to the declared timeout. Aborting inside that window
        still burns the slot and its cooldown, and returns nothing."""
        assert ov._TIMEOUT_HTTP > ov._TIMEOUT_QUERY + 15


class TestUserAgent:
    def test_identifies_the_real_repository(self):
        """A user agent an operator cannot verify invites the manual kind of ban,
        which does not expire on its own."""
        ua = ov._HEADERS["User-Agent"]
        assert "(+https://github.com/rui-nar/TraxJourney)" in ua

    def test_carries_a_version(self):
        assert ov._HEADERS["User-Agent"].startswith("TraxJourney/")


class TestBrokenHostsFailOverImmediately:
    """The other half: where moving on really is the faster path."""

    def test_connection_error_moves_on_without_waiting(self, transport):
        transport.script[_PRIMARY] = [OSError("connection refused")]
        transport.script[_FALLBACK] = [_Resp(200, payload={"elements": ["fb"]})]

        assert _overpass(_QUERY) == {"elements": ["fb"]}
        assert transport.sleeps == [], "a broken host must not be waited on"

    @pytest.mark.parametrize("status", [400, 404])
    def test_client_errors_move_on_without_cooling_the_host(self, transport, status):
        """A malformed query is about the request, not the host — do not punish
        an endpoint for our own mistake."""
        transport.script[_PRIMARY] = [_Resp(status)]
        transport.script[_FALLBACK] = [_Resp(200, payload={"elements": []})]

        _overpass(_QUERY)
        assert transport.calls == [_PRIMARY, _FALLBACK]

    def test_unparseable_body_moves_on(self, transport):
        """A 200 carrying an Overpass runtime-error page, which the QL
        [timeout:30] directive can produce well inside our socket timeout."""
        transport.script[_PRIMARY] = [_Resp(200, payload=None, text="<html>error")]
        transport.script[_FALLBACK] = [_Resp(200, payload={"elements": []})]

        _overpass(_QUERY)
        assert transport.calls == [_PRIMARY, _FALLBACK]



class TestFailureReporting:
    def test_error_names_every_endpoint_attempted(self, transport):
        """Phase 1 of the fix. The old message kept only the last error, so it
        named one mirror while claiming "all endpoints" — which is why the
        incident logs never showed what the primary actually returned."""
        transport.script[_PRIMARY] = [_Resp(429)]
        transport.script[_FALLBACK] = [TimeoutError("read timed out")]

        with pytest.raises(OverpassError) as err:
            _overpass(_QUERY)

        message = str(err.value)
        assert _PRIMARY in message and _FALLBACK in message
        assert "429" in message and "TimeoutError" in message

    def test_the_incident_cascade_is_reported_in_full(self, transport):
        """Every endpoint read-timing-out — the shape seen on 2026-09-06."""
        transport.script[_PRIMARY] = [TimeoutError("read timed out")]
        transport.script[_FALLBACK] = [TimeoutError("read timed out")]

        with pytest.raises(OverpassError) as err:
            _overpass(_QUERY)

        assert str(err.value).count("TimeoutError") == 2
        assert transport.sleeps == []


class TestPacing:
    def test_no_free_slot_moves_to_the_next_host(self, monkeypatch, transport):
        """When our own traffic is saturating a host, another endpoint has its
        own quota — queueing behind ourselves does not."""
        @contextmanager
        def _denied(name, _limit, **_kw):
            yield name != ov._slot_name(_PRIMARY)

        monkeypatch.setattr(ov, "slot", _denied)
        transport.script[_FALLBACK] = [_Resp(200, payload={"elements": []})]

        _overpass(_QUERY)
        assert transport.calls == [_FALLBACK]

    def test_dead_mirror_is_not_in_the_rotation(self):
        """kumi.systems answered nothing within 50s from the production VPS, so
        every query that reached it paid the full socket timeout to find out.

        private.coffee joined it on 2026-09-08: no answer to `out count;`
        within 90s, which is beyond _TIMEOUT_HTTP, so every attempt spent the
        whole timeout to learn what the previous one had already learned."""
        for dead in ("kumi", "private.coffee"):
            assert not any(dead in url for url in ov._OVERPASS_ENDPOINTS), dead

    def test_no_region_limited_instance_is_in_the_rotation(self):
        """The failure mode this guards is quieter than a dead host.

        A country-scoped instance answers 200 in 0.1s with an empty `elements`
        array — which is exactly what a correct query over a region with no
        rail returns. All three strategies read it as "no route found" and
        degrade to a straight line, and nothing in the logs says the data was
        never there. Measured from the VPS on 2026-09-08: overpass.osm.ch
        returns 0 stations for a Flensburg bbox where overpass.openstreetmap.fr
        returns 2. Liveness is not coverage; check both before adding one.
        """
        for host in ov._REGION_LIMITED_INSTANCES:
            assert not any(host in url for url in ov._OVERPASS_ENDPOINTS), host

    def test_at_least_one_endpoint_answers_planet_wide(self):
        """The list must never be emptied down to region-limited hosts alone."""
        assert ov._OVERPASS_ENDPOINTS
        assert not all(
            any(bad in url for bad in ov._REGION_LIMITED_INSTANCES)
            for url in ov._OVERPASS_ENDPOINTS
        )


class TestResponseCaching:
    """The demand-reduction half. A retry — by hand, or by the hourly degraded
    sweep — re-asks a byte-identical question, and before this every one of them
    went to the network. That repetition is what escalated a soft rate limit into
    an outright IPv4 block on 2026-09-06.
    """

    @pytest.fixture
    def cached(self, monkeypatch):
        import fakeredis

        from src.jobs import upstream_cache
        client = fakeredis.FakeRedis()
        monkeypatch.setattr(upstream_cache, "get_redis", lambda: client)
        return client

    def test_an_identical_query_is_not_re_requested(self, transport, cached):
        transport.script[_PRIMARY] = [_Resp(200, payload={"elements": [1]},
                                           text='{"elements": [1]}')]

        first = _overpass(_QUERY)
        second = _overpass(_QUERY)

        assert first == second == {"elements": [1]}
        assert transport.calls == [_PRIMARY], "the second call must be served from cache"

    def test_a_cache_hit_costs_no_slot_and_no_wait(self, transport, cached,
                                                   monkeypatch):
        """A hit must short-circuit before pacing: queueing behind our own
        concurrency limit to answer from memory would be absurd."""
        transport.script[_PRIMARY] = [_Resp(200, payload={"elements": []},
                                           text='{"elements": []}')]
        _overpass(_QUERY)

        def _must_not_acquire(*_a, **_kw):
            raise AssertionError("a cache hit must not take a slot")

        monkeypatch.setattr(ov, "slot", _must_not_acquire)
        assert _overpass(_QUERY) == {"elements": []}

    def test_a_different_query_still_goes_to_the_network(self, transport, cached):
        transport.script[_PRIMARY] = [
            _Resp(200, payload={"elements": ["a"]}, text='{"elements": ["a"]}'),
            _Resp(200, payload={"elements": ["b"]}, text='{"elements": ["b"]}'),
        ]

        assert _overpass("query one") == {"elements": ["a"]}
        assert _overpass("query two") == {"elements": ["b"]}
        assert transport.calls == [_PRIMARY, _PRIMARY]

    def test_failures_are_not_cached(self, transport, cached):
        """Only a parsed success is stored. Caching a 429 or a timeout would
        turn a transient upstream problem into a day-long one — and once the
        hosts finish cooling, the next attempt must go back to the network."""
        transport.script[_PRIMARY] = [_Resp(504)]
        transport.script[_FALLBACK] = [TimeoutError("nope")]

        with pytest.raises(OverpassError):
            _overpass(_QUERY)

        assert list(cached.scan_iter("cache:overpass:*")) == []


class TestPolitenessBound:
    def test_only_one_overpass_request_at_a_time(self):
        """Matching the advertised cap of 2 exactly left no margin and 429'd
        constantly — and a rejection costs 10-14s, because the dispatcher queues
        you before refusing. One is deliberate, not a typo."""
        assert ov._OVERPASS_CONCURRENCY == 1
