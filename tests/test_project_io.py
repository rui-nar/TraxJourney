"""Tests for project data models and ProjectIO round-trip — no Qt required."""

import json
import os
import pytest

from src.models.encounter import Encounter
from src.models.journal import JournalEntry
from src.models.memory import Memory
from src.models.project import (
    ConnectingSegment,
    Project,
    ProjectFilterState,
    ProjectItem,
    SegmentEndpoint,
)
from src.project.project_io import InvalidProjectFile, ProjectIO


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_activity_item(aid: int) -> ProjectItem:
    return ProjectItem(item_type="activity", activity_id=aid)


def _make_segment_item(label: str = "Paris → Lyon") -> ProjectItem:
    seg = ConnectingSegment(
        id="test-uuid-1234",
        segment_type="train",
        label=label,
        start=SegmentEndpoint(lat=48.85, lon=2.35, source="auto"),
        end=SegmentEndpoint(lat=45.74, lon=4.83, source="manual"),
    )
    return ProjectItem(item_type="segment", segment=seg)


def _make_journal_item(description: str = "Great day") -> ProjectItem:
    entry = JournalEntry(
        id=7,
        date="2024-05-01",
        time="09:30",
        description=description,
        photos=["00000000-0000-4000-8000-000000000033"],
        geo_mode="custom",
        lat=48.85,
        lon=2.35,
    )
    return ProjectItem(item_type="journal", journal=entry)


@pytest.fixture
def simple_project():
    p = Project(name="Test Journey")
    p.items = [
        _make_activity_item(111),
        _make_segment_item(),
        _make_activity_item(222),
    ]
    p.filter_state = ProjectFilterState(
        start_date="2024-01-01",
        end_date="2024-12-31",
        activity_types=["Run", "Ride"],
    )
    return p


# ---------------------------------------------------------------------------
# ProjectItem / ConnectingSegment
# ---------------------------------------------------------------------------

class TestProjectItem:
    def test_activity_item(self):
        item = _make_activity_item(42)
        assert item.item_type == "activity"
        assert item.activity_id == 42
        assert item.segment is None

    def test_segment_item(self):
        item = _make_segment_item()
        assert item.item_type == "segment"
        assert item.segment is not None
        assert item.segment.segment_type == "train"
        assert item.segment.start.lat == pytest.approx(48.85)
        assert item.segment.end.source == "manual"

    def test_segment_default_id_is_uuid(self):
        seg = ConnectingSegment()
        assert len(seg.id) == 36    # UUID4 canonical form
        assert seg.id.count("-") == 4


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------

class TestProjectHelpers:
    def test_move_item_forward(self):
        p = Project(name="x")
        p.items = [_make_activity_item(i) for i in range(4)]
        p.move_item(0, 2)
        assert [i.activity_id for i in p.items] == [1, 2, 0, 3]

    def test_move_item_backward(self):
        p = Project(name="x")
        p.items = [_make_activity_item(i) for i in range(4)]
        p.move_item(3, 1)
        assert [i.activity_id for i in p.items] == [0, 3, 1, 2]

    def test_move_item_no_op(self):
        p = Project(name="x")
        p.items = [_make_activity_item(i) for i in range(3)]
        p.move_item(1, 1)
        assert [i.activity_id for i in p.items] == [0, 1, 2]

    def test_remove_item(self):
        p = Project(name="x")
        p.items = [_make_activity_item(i) for i in range(3)]
        p.remove_item(1)
        assert [i.activity_id for i in p.items] == [0, 2]

    def test_remove_out_of_range_noop(self):
        p = Project(name="x")
        p.items = [_make_activity_item(0)]
        p.remove_item(99)   # should not raise
        assert len(p.items) == 1


# ---------------------------------------------------------------------------
# ProjectIO serialisation helpers (unit-level, no disk)
# ---------------------------------------------------------------------------

class TestProjectIOHelpers:
    def test_serialise_activity_item(self):
        item = _make_activity_item(99)
        d = ProjectIO._serialise_item(item)
        assert d == {"item_type": "activity", "activity_id": 99}

    def test_serialise_segment_item(self):
        item = _make_segment_item("Test label")
        d = ProjectIO._serialise_item(item)
        assert d["item_type"] == "segment"
        seg_d = d["segment"]
        assert seg_d["segment_type"] == "train"
        assert seg_d["label"] == "Test label"
        assert seg_d["start"]["lat"] == pytest.approx(48.85)
        assert seg_d["end"]["source"] == "manual"

    def test_deserialise_activity_item(self):
        d = {"item_type": "activity", "activity_id": 77}
        item = ProjectIO._deserialise_item(d)
        assert item.item_type == "activity"
        assert item.activity_id == 77

    def test_deserialise_segment_item(self):
        d = {
            "item_type": "segment",
            "segment": {
                "id": "abc",
                "segment_type": "flight",
                "label": "NYC → LAX",
                "start": {"lat": 40.71, "lon": -74.01, "source": "auto"},
                "end":   {"lat": 34.05, "lon": -118.24, "source": "manual"},
            },
        }
        item = ProjectIO._deserialise_item(d)
        assert item.item_type == "segment"
        assert item.segment.segment_type == "flight"
        assert item.segment.label == "NYC → LAX"
        assert item.segment.start.lon == pytest.approx(-74.01)

    def test_round_trip_item_activity(self):
        orig = _make_activity_item(55)
        restored = ProjectIO._deserialise_item(ProjectIO._serialise_item(orig))
        assert restored.item_type == orig.item_type
        assert restored.activity_id == orig.activity_id

    def test_round_trip_item_segment(self):
        orig = _make_segment_item("round trip")
        restored = ProjectIO._deserialise_item(ProjectIO._serialise_item(orig))
        assert restored.segment.label == "round trip"
        assert restored.segment.segment_type == "train"
        assert restored.segment.start.lat == pytest.approx(48.85)

    def test_serialise_journal_item(self):
        item = _make_journal_item("Serialise me")
        d = ProjectIO._serialise_item(item)
        assert d["item_type"] == "journal"
        j_d = d["journal"]
        assert j_d["id"] == 7
        assert j_d["date"] == "2024-05-01"
        assert j_d["time"] == "09:30"
        assert j_d["description"] == "Serialise me"
        assert j_d["photos"] == ["00000000-0000-4000-8000-000000000033"]
        assert j_d["geo_mode"] == "custom"
        assert j_d["lat"] == pytest.approx(48.85)
        assert j_d["lon"] == pytest.approx(2.35)

    def test_deserialise_journal_item(self):
        d = {
            "item_type": "journal",
            "journal": {
                "id": 9,
                "date": "2024-06-15",
                "time": "14:00",
                "description": "Deserialise me",
                "photos": ["00000000-0000-4000-8000-000000000031", "00000000-0000-4000-8000-000000000032"],
                "geo_mode": "start_of_day",
                "lat": 45.0,
                "lon": 5.0,
            },
        }
        item = ProjectIO._deserialise_item(d)
        assert item.item_type == "journal"
        assert item.journal.id == 9
        assert item.journal.description == "Deserialise me"
        assert item.journal.photos == ["00000000-0000-4000-8000-000000000031", "00000000-0000-4000-8000-000000000032"]
        assert item.journal.lat == pytest.approx(45.0)

    def test_round_trip_item_journal(self):
        orig = _make_journal_item("round trip journal")
        restored = ProjectIO._deserialise_item(ProjectIO._serialise_item(orig))
        assert restored.item_type == "journal"
        assert restored.journal.description == "round trip journal"
        assert restored.journal.date == "2024-05-01"
        assert restored.journal.lat == pytest.approx(48.85)


# ---------------------------------------------------------------------------
# Full file round-trip
# ---------------------------------------------------------------------------

class TestProjectIORoundTrip:
    def test_save_and_load(self, simple_project, tmp_path):
        path = str(tmp_path / "test.traxj")
        ProjectIO.save(simple_project, path)
        assert os.path.exists(path)

        loaded = ProjectIO.load(path)
        assert loaded.name == "Test Journey"
        assert loaded.version == 1
        assert len(loaded.items) == 3

    def test_items_preserved(self, simple_project, tmp_path):
        path = str(tmp_path / "test.traxj")
        ProjectIO.save(simple_project, path)
        loaded = ProjectIO.load(path)

        assert loaded.items[0].item_type == "activity"
        assert loaded.items[0].activity_id == 111
        assert loaded.items[1].item_type == "segment"
        assert loaded.items[1].segment.label == "Paris → Lyon"
        assert loaded.items[2].item_type == "activity"
        assert loaded.items[2].activity_id == 222

    def test_filter_state_preserved(self, simple_project, tmp_path):
        path = str(tmp_path / "test.traxj")
        ProjectIO.save(simple_project, path)
        loaded = ProjectIO.load(path)

        assert loaded.filter_state.start_date == "2024-01-01"
        assert loaded.filter_state.end_date == "2024-12-31"
        assert loaded.filter_state.activity_types == ["Run", "Ride"]

    def test_segment_coordinates_preserved(self, simple_project, tmp_path):
        path = str(tmp_path / "test.traxj")
        ProjectIO.save(simple_project, path)
        loaded = ProjectIO.load(path)

        seg = loaded.items[1].segment
        assert seg.start.lat == pytest.approx(48.85)
        assert seg.start.lon == pytest.approx(2.35)
        assert seg.end.lat == pytest.approx(45.74)
        assert seg.end.source == "manual"

    def test_empty_project_round_trip(self, tmp_path):
        p = Project(name="Empty")
        path = str(tmp_path / "empty.traxj")
        ProjectIO.save(p, path)
        loaded = ProjectIO.load(path)
        assert loaded.name == "Empty"
        assert loaded.items == []
        assert loaded.activities == []

    def test_file_is_valid_json(self, simple_project, tmp_path):
        import json
        path = str(tmp_path / "test.traxj")
        ProjectIO.save(simple_project, path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert "version" in data
        assert "name" in data
        assert "items" in data
        assert "activities" in data

    def test_load_refuses_a_malformed_activity(self, tmp_path):
        """Issue #205: a corrupt activity entry used to be dropped by
        ProjectIO.load() with zero trace (`except Exception: pass`), then
        dropped with a warning. A value no writer emits now refuses the whole
        file instead (issue #462): importing the rest of the trip without it
        loses the activity as silently as before, as far as the user sees."""
        path = str(tmp_path / "corrupt.traxj")
        data = {
            "name": "Test",
            "version": 1,
            # Every writer emits items; a document without them is refused
            # as not a trip (issue #451).
            "items": [],
            "activities": [
                {
                    "id": 1, "name": "Ride", "type": "Ride",
                    "start_date": "2024-01-01T00:00:00Z",
                    "start_date_local": "2024-01-01T00:00:00Z",
                },
                {"id": 2, "start_date": "not-a-date"},
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        with pytest.raises(InvalidProjectFile, match=r"activities\[1\]\.start_date "):
            ProjectIO.load(path)

    def test_unicode_in_label(self, tmp_path):
        p = Project(name="München → Paris 🚂")
        seg = ConnectingSegment(
            id="u1", segment_type="train", label="Basel → Zürich ✓",
            start=SegmentEndpoint(47.56, 7.59),
            end=SegmentEndpoint(47.38, 8.54),
        )
        p.items.append(ProjectItem(item_type="segment", segment=seg))
        path = str(tmp_path / "unicode.traxj")
        ProjectIO.save(p, path)
        loaded = ProjectIO.load(path)
        assert loaded.name == "München → Paris 🚂"
        assert loaded.items[0].segment.label == "Basel → Zürich ✓"


# ---------------------------------------------------------------------------
# Dataclass to_dict()/from_dict() round-trips
# ---------------------------------------------------------------------------

class TestDataclassRoundTrips:
    def test_memory_round_trip(self):
        mem = Memory(
            id=1,
            public_id="pub-123",
            name="Lunch stop",
            date="2024-03-10",
            time="12:30",
            description="Great pizza",
            photos=["a.jpg", "b.jpg"],
            geo_mode="custom",
            lat=41.9,
            lon=12.5,
            comment_count=3,
            like_count=5,
        )
        restored = Memory.from_dict(mem.to_dict())
        assert restored.id == mem.id
        assert restored.public_id == mem.public_id
        assert restored.name == mem.name
        assert restored.date == mem.date
        assert restored.time == mem.time
        assert restored.description == mem.description
        assert restored.photos == mem.photos
        assert restored.geo_mode == mem.geo_mode
        assert restored.lat == pytest.approx(mem.lat)
        assert restored.lon == pytest.approx(mem.lon)

    def test_journal_entry_round_trip(self):
        entry = JournalEntry(
            id=2,
            date="2024-03-11",
            time="08:00",
            description="Morning notes",
            photos=["c.jpg"],
            geo_mode="end_of_day",
            lat=45.1,
            lon=7.7,
        )
        restored = JournalEntry.from_dict(entry.to_dict())
        assert restored.id == entry.id
        assert restored.date == entry.date
        assert restored.time == entry.time
        assert restored.description == entry.description
        assert restored.photos == entry.photos
        assert restored.geo_mode == entry.geo_mode
        assert restored.lat == pytest.approx(entry.lat)
        assert restored.lon == pytest.approx(entry.lon)

    def test_encounter_round_trip(self):
        enc = Encounter(
            id=3,
            person_id=42,
            group_id=None,
            date="2024-03-12",
            time="17:15",
            description="Met at the hostel",
            geo_mode="start_of_day",
            lat=48.2,
            lon=16.4,
        )
        restored = Encounter.from_dict(enc.to_dict())
        assert restored.id == enc.id
        assert restored.person_id == enc.person_id
        assert restored.group_id == enc.group_id
        assert restored.date == enc.date
        assert restored.time == enc.time
        assert restored.description == enc.description
        assert restored.geo_mode == enc.geo_mode
        assert restored.lat == pytest.approx(enc.lat)
        assert restored.lon == pytest.approx(enc.lon)

    def test_encounter_round_trip_group_id(self):
        """issue #56 — group_id is the alternative to person_id, must round-trip too."""
        enc = Encounter(
            id=4,
            person_id=None,
            group_id=17,
            date="2024-03-13",
            description="Met the whole crew",
        )
        restored = Encounter.from_dict(enc.to_dict())
        assert restored.person_id is None
        assert restored.group_id == 17

    def test_connecting_segment_round_trip(self):
        seg = ConnectingSegment(
            id="seg-1",
            segment_type="boat",
            label="Piraeus → Naxos",
            date="2024-03-14",
            start=SegmentEndpoint(lat=37.9, lon=23.6, source="auto"),
            end=SegmentEndpoint(lat=37.1, lon=25.4, source="manual"),
            route_mode="ferry",
            train_number=None,
            hafas_provider=None,
            route_polyline="encoded-polyline",
            route_status="resolved",
            route_error=None,
            route_started_at="2024-03-14T10:00:00",
            route_degraded=True,
            route_degrade_retries=2,
        )
        restored = ConnectingSegment.from_dict(seg.to_dict())
        assert restored.id == seg.id
        assert restored.segment_type == seg.segment_type
        assert restored.label == seg.label
        assert restored.date == seg.date
        assert restored.start.lat == pytest.approx(seg.start.lat)
        assert restored.start.source == seg.start.source
        assert restored.end.lat == pytest.approx(seg.end.lat)
        assert restored.end.source == seg.end.source
        assert restored.route_mode == seg.route_mode
        assert restored.route_polyline == seg.route_polyline
        assert restored.route_status == seg.route_status
        assert restored.route_started_at == seg.route_started_at
        # Regression: from_dict() used to drop route_degraded entirely, so any
        # code loading a project via ProjectRepo.get_project() saw False
        # regardless of what was actually stored (issue #207).
        assert restored.route_degraded is True
        assert restored.route_degrade_retries == 2

    def test_connecting_segment_defaults_are_not_degraded(self):
        """A segment that was never resolved must not look like a stuck retry."""
        restored = ConnectingSegment.from_dict({})
        assert restored.route_degraded is False
        assert restored.route_degrade_retries == 0

    # ── Resolver provenance (issue #364) ─────────────────────────────────────

    def test_connecting_segment_provenance_round_trips(self):
        seg = ConnectingSegment(
            id="seg-1", segment_type="train",
            route_resolver_version=3, route_strategy="relation_uic",
        )
        restored = ConnectingSegment.from_dict(seg.to_dict())
        assert restored.route_resolver_version == 3
        assert restored.route_strategy == "relation_uic"

    def test_a_segment_written_before_the_stamp_reads_as_version_zero(self):
        """Deliberate, and the choice the whole issue turns on: an absent stamp
        means "produced by a resolver older than any we stamped", so the stale
        sweep reaches exactly the rows #359 drew wrongly. Reading it as
        "unknown, leave it alone" would have the mechanism reach nothing at all
        on the day it shipped and only ever help future bugs.
        """
        restored = ConnectingSegment.from_dict({"id": "s", "route_status": "resolved"})
        assert restored.route_resolver_version == 0
        assert restored.route_strategy is None

    def test_the_db_row_mapper_carries_the_stamp_too(self):
        """ProjectRepo reads segments through its own mapper, not from_dict, and
        anything that mapper omits is *destroyed* rather than merely unread:
        ``save_project`` writes every item's payload back from ``to_dict()``, so
        a dropped field returns as its dataclass default on the next reorder.

        For the stamp that would mean reverting a segment to version 0 every
        time the user moved an item around, and the stale sweep re-resolving it
        — the same re-resolve treadmill RESOLVER_VERSION's bump rule exists to
        prevent, reached by editing a trip rather than by bumping a constant.
        """
        from src.project.repo_row_mappers import RowMappersMixin

        stored = json.dumps(ConnectingSegment(
            id="seg-1", segment_type="train", route_status="resolved",
            route_resolver_version=2, route_strategy="coordinate_dijkstra",
            route_degrade_retries=4,
        ).to_dict())
        seg = RowMappersMixin._json_to_segment(stored)
        assert seg.route_resolver_version == 2
        assert seg.route_strategy == "coordinate_dijkstra"
        # Same omission, same consequence: the degraded retry budget was being
        # silently reset to 0 by every structural save (issue #207's counter).
        assert seg.route_degrade_retries == 4


# ---------------------------------------------------------------------------
# strava_id, split family and edit snapshot (#592)
# ---------------------------------------------------------------------------

def _activity_dict(aid, **extra):
    return {
        "id": aid, "name": "Ride", "type": "Ride",
        "start_date": "2024-01-01T00:00:00Z",
        "start_date_local": "2024-01-01T00:00:00Z",
        **extra,
    }


def _trip_with(*activities):
    return {"name": "T", "version": 1, "items": [], "activities": list(activities)}


def _split_family():
    """A Strava activity cut in two, then its head edited again: the head
    holds a split family and a first-edit snapshot, the tail is local."""
    from src.models.activity import Activity
    head = Activity.from_strava_api(_activity_dict(
        7_001, name="Alps (1/2)", strava_id=123_456_789, is_edited=True,
        split_root_id=None, split_base_name="Alps",
        original_polyline="_p~iF~ps|U_ulLnnqC",
        original_elevation_profile={"distances_km": [0.0, 1.5], "elevations_m": [400.0, 820.5]},
        original_start_latlng=[45.9, 6.8], original_end_latlng=[46.0, 6.9],
        original_total_elevation_gain=420.5, original_distance=1500.0,
        original_moving_time=900, original_elapsed_time=1200,
        original_average_speed=1.67, original_elev_high=820.5, original_elev_low=400.0,
    ))
    tail = Activity.from_strava_api(_activity_dict(
        -9_002, name="Alps (2/2)", strava_id=None, is_edited=True,
        split_root_id=7_001, split_parent_id=7_001,
    ))
    return head, tail


class TestStravaIdAndSnapshot:
    def test_strava_id_round_trips_through_a_file(self, tmp_path):
        """The row id names a row of one trip; the Strava activity is
        strava_id, and it survives a save and load whatever the id is."""
        head, tail = _split_family()
        p = Project(name="T", activities=[head, tail])
        path = str(tmp_path / "t.traxj")
        ProjectIO.save(p, path)

        loaded = {a.id: a for a in ProjectIO.load(path).activities}
        assert loaded[7_001].strava_id == 123_456_789
        assert loaded[-9_002].strava_id is None

    def test_a_null_strava_id_is_taken_at_its_word(self):
        """A file since #592 says a positive id with no strava_id is local."""
        project = ProjectIO.from_dict(_trip_with(_activity_dict(55, strava_id=None)))
        assert project.activities[0].strava_id is None

    @pytest.mark.parametrize("source", [None, "", "strava"])
    def test_an_old_files_positive_strava_id_becomes_the_strava_id(self, source):
        """Before #592 a Strava activity's id was its Strava id (Decision 8)."""
        project = ProjectIO.from_dict(_trip_with(_activity_dict(987_654_321, source=source)))
        assert project.activities[0].strava_id == 987_654_321

    @pytest.mark.parametrize("aid, source", [
        (-42, None),        # imported from another account's file (review R1-7)
        (-43, "gpx"),
        (44, "gpx"),
    ])
    def test_an_old_files_local_activity_has_no_strava_id(self, aid, source):
        project = ProjectIO.from_dict(_trip_with(_activity_dict(aid, source=source)))
        assert project.activities[0].strava_id is None

    def test_a_split_family_and_edit_snapshot_survive_a_round_trip(self, tmp_path):
        head, tail = _split_family()
        p = Project(name="T", activities=[head, tail])
        path = str(tmp_path / "t.traxj")
        ProjectIO.save(p, path)

        loaded = {a.id: a for a in ProjectIO.load(path).activities}
        h, t = loaded[7_001], loaded[-9_002]
        assert (t.split_root_id, t.split_parent_id) == (7_001, 7_001)
        assert h.split_base_name == "Alps"
        assert h.is_edited and t.is_edited
        assert h.original_polyline == "_p~iF~ps|U_ulLnnqC"
        assert h.original_elevation_profile == ([0.0, 1.5], [400.0, 820.5])
        assert h.original_start_latlng == [45.9, 6.8]
        assert h.original_end_latlng == [46.0, 6.9]
        assert (h.original_total_elevation_gain, h.original_distance,
                h.original_moving_time, h.original_elapsed_time,
                h.original_average_speed, h.original_elev_high,
                h.original_elev_low) == (420.5, 1500.0, 900, 1200, 1.67, 820.5, 400.0)

    def test_an_encrypted_snapshot_survives_a_round_trip(self, tmp_path):
        from src.models.activity import Activity
        env = "v1.QUJD.REVG"
        act = Activity.from_strava_api(_activity_dict(
            8, strava_id=8, is_edited=True, original_polyline=env,
            original_elevation_profile_enc=env,
            original_start_latlng_enc=env, original_end_latlng_enc=env))
        path = str(tmp_path / "t.traxj")
        ProjectIO.save(Project(name="T", activities=[act]), path)

        back = ProjectIO.load(path).activities[0]
        assert back.original_polyline == env
        assert back.original_elevation_profile is None
        assert (back.original_elevation_profile_enc, back.original_start_latlng_enc,
                back.original_end_latlng_enc) == (env, env, env)

    def test_the_rest_api_adds_strava_id_and_nothing_else(self):
        """Decision 11: activity dicts gain strava_id only; the split family
        and the snapshot stay in the file."""
        head, _ = _split_family()
        d = ProjectIO.to_dict(Project(name="T", activities=[head]))["activities"][0]
        assert d["strava_id"] == 123_456_789
        assert not any(k.startswith("original_") for k in d)
        assert "split_root_id" not in d and "split_base_name" not in d

    @pytest.mark.parametrize("value, says", [
        (0, "is out of range"),
        (-5, "is out of range"),
        ("123", "is not a whole number"),
        (True, "is not a whole number"),
    ])
    def test_a_strava_id_no_writer_emits_is_refused(self, value, says):
        with pytest.raises(InvalidProjectFile,
                           match=rf"activities\[0\]\.strava_id {says}"):
            ProjectIO.from_dict(_trip_with(_activity_dict(1, strava_id=value)))

    @pytest.mark.parametrize("field, value", [
        ("original_start_latlng", [95.0, 0.0]),
        ("original_distance", -1.0),
        ("original_elevation_profile_enc", "not an envelope"),
        ("split_root_id", "1"),
    ])
    def test_a_snapshot_value_no_writer_emits_is_refused(self, field, value):
        with pytest.raises(InvalidProjectFile, match=rf"activities\[0\]\.{field}"):
            ProjectIO.from_dict(_trip_with(_activity_dict(1, **{field: value})))

    def test_a_snapshot_is_repaired_as_the_live_figures_are(self):
        """A snapshot taken from a row before #462 can hold an implausible
        figure; its export must still import, as a live one does."""
        project = ProjectIO.from_dict(_trip_with(_activity_dict(
            1, original_elapsed_time=10 ** 12, original_moving_time=600,
            original_elev_high=65535.0,
            original_elevation_profile={"distances_km": [0.0, 1.0],
                                        "elevations_m": [100.0, 140.0]})))
        act = project.activities[0]
        assert act.original_elapsed_time == 600
        assert act.original_elev_high == 140.0
