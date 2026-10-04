"""An imported trip keeps the settings its file carries (issue #465).

The .traxj export writes a trip's settings as well as its content: its dates,
sleeping options and their groups, counters and each day's counts, track and
elevation-chart style, per-type styles and translation languages. The import
read none of them but the sleeping options, so an export imported back (a new
trip, or #452's "Keep both" copy) silently lost them.

Each is now read, checked by the trip-file schema (src/project/traxj_schema.py)
and stored. What a file never carries stays the server's own: the trip's name
(the uploaded file's), its lock version, share links, companions, sync
settings and cached totals.

Replace (#452) takes the file's settings too, since it means "overwrite this
trip with the file": a setting the file carries wins, and one it does not
carry (an older export, the ZIP export's trip file) is kept as the trip has
it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.project.project_io import ProjectIO
from tests.test_elevation_non_finite import app  # noqa: F401

_HISTORY = Path(__file__).parent / "fixtures" / "traxj_history"

#: The settings a trip's JSON shows, and so what a round trip must keep.
_SETTINGS = (
    "trip_start", "trip_end", "day_meta", "sleeping_options",
    "sleeping_option_groups", "counters", "track_color", "track_secondary_color",
    "track_width", "alternating_track_colors", "elevation_chart_color",
    "elevation_chart_show_line", "color_by_type", "type_styles", "languages",
)


def _settings(client, name: str) -> dict:
    r = client.get(f"/api/projects/{name}")
    assert r.status_code == 200, r.text
    trip = r.json()
    return {k: trip[k] for k in _SETTINGS}


def _style(client, name, **style):
    r = client.put(f"/api/projects/{name}/track-style", json=style)
    assert r.status_code == 204, r.text


def _set_everything(client, name="Trip"):
    """Every setting away from its default."""
    assert client.put(f"/api/projects/{name}", json={
        "trip_start": "2024-06-01", "trip_end": "2024-06-20"}).status_code == 200
    _style(client, name, track_color="#123456", track_secondary_color="#654321",
           track_width=4.5, alternating_track_colors=True, elevation_chart_color="#00ff00",
           elevation_chart_show_line=False, color_by_type=True,
           type_styles={"ride": {"color": "#ABCDEF", "style": "dashed"},
                        "train": {"color": "#16A34A"}})
    assert client.put(f"/api/projects/{name}/languages",
                      json={"languages": ["fr", "de"]}).status_code == 204
    assert client.patch(f"/api/projects/{name}/day-meta", json={
        "days": {"2024-06-02": {"journal": "Big day", "tags": ["alps"],
                                    "sleeping": "Hut",
                                    "counters": [{"name": "Coffee", "value": 2},
                                                 {"name": "Coffee", "value": 1.5}]}},
        "sleeping_options": ["Hut", "Tent", "Barn"],
        "sleeping_option_groups": {"Hut": "Indoors", "Tent": "Outdoors", "Barn": "Other"},
        "counters": [{"name": "Coffee", "start": 5}, {"name": "Punctures", "start": 0}],
    }).status_code == 200


def _export(client, name="Trip") -> bytes:
    r = client.get(f"/api/projects/{name}/export-traxj")
    assert r.status_code == 200, r.text
    return r.content


def _import(client, filename, content, **params):
    return client.post("/api/projects/import", params=params, files={
        "file": (f"{filename}{ProjectIO.EXTENSION}", content, "application/json")})


# ── Round trips ─────────────────────────────────────────────────────────────

def test_every_setting_survives_a_round_trip(app):  # noqa: F811
    client, _ = app
    _set_everything(client)
    before = _settings(client, "Trip")

    r = _import(client, "Copy", _export(client))

    assert r.status_code == 201, r.text
    assert _settings(client, "Copy") == before


def test_every_setting_survives_keep_both(app):  # noqa: F811
    client, _ = app
    _set_everything(client)
    before = _settings(client, "Trip")

    r = _import(client, "Trip", _export(client), on_conflict="copy")

    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "copied"
    assert _settings(client, r.json()["name"]) == before


def test_replace_takes_the_file_s_settings(app):  # noqa: F811
    client, _ = app
    _set_everything(client)
    exported, wanted = _export(client), _settings(client, "Trip")
    assert client.post("/api/projects", json={"name": "Other"}).status_code == 201
    _style(client, "Other", track_color="#000000", track_width=1.0)
    assert client.put("/api/projects/Other/languages", json={"languages": ["pt"]}).status_code == 204

    r = _import(client, "Other", exported, on_conflict="replace")

    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "replaced"
    assert _settings(client, "Other") == wanted


def test_replace_keeps_a_setting_the_file_does_not_carry(app):  # noqa: F811
    """An older export, or the ZIP export's trip file, carries no style: the
    trip keeps its own rather than falling back to the defaults."""
    client, _ = app
    _set_everything(client)
    before = _settings(client, "Trip")
    doc = json.loads(_export(client))
    for key in ("trip_end", "counters", "sleeping_option_groups", "track_color",
                "track_secondary_color", "track_width", "alternating_track_colors",
                "elevation_chart_color", "elevation_chart_show_line", "color_by_type",
                "type_styles", "languages"):
        doc.pop(key)

    r = _import(client, "Trip", json.dumps(doc).encode(), on_conflict="replace")

    assert r.status_code == 201, r.text
    after = _settings(client, "Trip")
    for key in ("trip_end", "counters", "sleeping_option_groups", "track_color",
                "track_secondary_color", "track_width", "alternating_track_colors",
                "elevation_chart_color", "elevation_chart_show_line", "color_by_type",
                "type_styles", "languages"):
        assert after[key] == before[key], key


def test_a_file_without_settings_imports_with_the_defaults(app):  # noqa: F811
    client, _ = app

    r = _import(client, "Bare", json.dumps({"version": 1, "items": []}).encode())

    assert r.status_code == 201, r.text
    s = _settings(client, "Bare")
    assert (s["trip_start"], s["trip_end"], s["counters"], s["languages"]) == (None, None, [], [])
    assert (s["track_color"], s["track_width"], s["color_by_type"]) == ("#F97316", 2.5, False)


# ── Files past versions wrote ───────────────────────────────────────────────

def test_an_older_export_s_settings_are_restored(app):  # noqa: F811
    """Written while each day's counters were a {name: value} map."""
    client, _ = app
    raw = (_HISTORY / "2026-05-31_bd73668e_counters_journal.traxj").read_bytes()

    r = _import(client, "Old", raw)

    assert r.status_code == 201, r.text
    s = _settings(client, "Old")
    assert (s["trip_start"], s["trip_end"]) == ("2026-03-01", "2026-03-09")
    assert s["languages"] == ["en", "fr"]
    assert s["counters"] == [{"name": "Coffee", "start": 0.0}, {"name": "Punctures", "start": 0.0}]
    assert s["sleeping_option_groups"]["Warmshower"] == "Indoors"
    assert s["day_meta"]["2026-03-01"]["counters"] == [
        {"name": "Coffee", "value": 2.0}, {"name": "Punctures", "value": 1.0}]


# ── Checked like the rest of the file ───────────────────────────────────────

def _file(**settings) -> bytes:
    return json.dumps({"version": 1, "items": [], **settings}).encode()


@pytest.mark.parametrize("settings, field", [
    ({"counters": [{"start": 1}]}, "counters[0].name"),
    ({"counters": [{"name": "Coffee", "start": "5"}]}, "counters[0].start"),
    ({"counters": {"Coffee": 5}}, "counters"),
    ({"day_meta": {"2024-06-01": {"counters": [{"name": "Coffee", "value": "2"}]}}},
     "day_meta.<day>.counters"),
    ({"day_meta": {"2024-06-01": {"counters": {"Coffee": "two"}}}}, "day_meta.<day>.counters"),
    ({"track_width": "wide"}, "track_width"),
    ({"alternating_track_colors": "yes"}, "alternating_track_colors"),
    ({"type_styles": {"ride": "blue"}}, "type_styles.<key>"),
    ({"languages": [1]}, "languages[0]"),
    ({"sleeping_option_groups": {"Hut": 3}}, "sleeping_option_groups.<key>"),
    ({"trip_end": 20240620}, "trip_end"),
], ids=["counter without a name", "counter start text", "counters a map",
        "day counter text", "day counter map of text", "width text", "flag text",
        "type style text", "language number", "group number", "trip end number"])
def test_a_setting_of_the_wrong_type_is_refused(app, settings, field):  # noqa: F811
    client, _ = app

    r = _import(client, "Bad", _file(**settings))

    assert r.status_code == 400, r.text
    assert f": {field} " in r.json()["detail"], r.json()["detail"]


def test_a_colour_or_width_the_client_cannot_draw_falls_back_to_the_default(app):  # noqa: F811
    """The settings API took any text and any width, so a past trip may hold
    one: it is not refused, but it is not kept either."""
    client, _ = app

    r = _import(client, "Odd", _file(track_color="orange", track_secondary_color="#12345",
                                     elevation_chart_color="#GGGGGG", track_width=1e6))

    assert r.status_code == 201, r.text
    s = _settings(client, "Odd")
    assert (s["track_color"], s["track_secondary_color"], s["elevation_chart_color"],
            s["track_width"]) == ("#F97316", None, None, 2.5)
