"""The client's Dart track maths stays pinned to the Python (issue #366).

The client edits encrypted tracks on the device with a Dart port of
``src/models/track_edit.py`` (``flutter_client/lib/src/track_metrics/``).
Python is the source of truth, held to it two ways from server CI, where no
Flutter toolchain exists:

* ``flutter_client/test/fixtures/track_metrics_vectors.json``, which the Dart
  tests replay, must be what ``scripts/gen_track_metrics_vectors.py`` produces
  from today's Python. Change the maths or a constant and this fails until the
  vectors are regenerated — and then the Dart tests fail until the port follows.
* The port's constants, parsed out of ``elevation_gain.dart`` with a regex, must
  equal Python's. Skipped only while that file does not exist yet.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from scripts.gen_track_metrics_vectors import OUTPUT, build_vectors, python_constants, render

_ROOT = Path(__file__).resolve().parent.parent
DART_SOURCE = (_ROOT / "flutter_client" / "lib" / "src" / "track_metrics"
               / "elevation_gain.dart")

#: Floats are compared within this, relative and absolute: the generator's
#: haversine goes through the platform's libm, which may differ in the last bit
#: between the machine that committed the file and CI.
_FLOAT_TOLERANCE = 1e-9


def _assert_same(committed: Any, fresh: Any, path: str = "$") -> None:
    assert type(committed) is type(fresh), (
        f"{path}: {type(committed).__name__} committed, "
        f"{type(fresh).__name__} from Python")
    if isinstance(fresh, dict):
        assert sorted(committed) == sorted(fresh), f"{path}: keys differ"
        for key in fresh:
            _assert_same(committed[key], fresh[key], f"{path}.{key}")
    elif isinstance(fresh, list):
        assert len(committed) == len(fresh), f"{path}: length differs"
        for i, (a, b) in enumerate(zip(committed, fresh)):
            label = a.get("name", i) if isinstance(a, dict) else i
            _assert_same(a, b, f"{path}[{label}]")
    elif isinstance(fresh, float):
        assert math.isclose(committed, fresh, rel_tol=_FLOAT_TOLERANCE,
                            abs_tol=_FLOAT_TOLERANCE), (
            f"{path}: {committed!r} committed, {fresh!r} from Python")
    else:
        assert committed == fresh, f"{path}: {committed!r} committed, {fresh!r} from Python"


def test_committed_vectors_match_python():
    committed = json.loads(OUTPUT.read_text(encoding="utf-8"))
    # Through the same serialiser as the file, so tuples are lists and so on.
    fresh = json.loads(render(build_vectors()))
    try:
        _assert_same(committed, fresh)
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}\n{OUTPUT.relative_to(_ROOT)} is stale: run "
            "`python scripts/gen_track_metrics_vectors.py` and update the Dart "
            "port (flutter_client/lib/src/track_metrics/) until its tests pass"
        ) from None


# ── Dart constants ────────────────────────────────────────────────────────────

_NUMBER = r"-?[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9]+)?"


def _dart_number(literal: str) -> float | int:
    text = literal.replace("_", "")
    if re.fullmatch(r"-?[0-9]+", text):
        return int(text)
    return float(text)


def parse_dart_constants(dart_source: str, names) -> Dict[str, Any]:
    """``{name: value}`` for each ``const`` named in *names* found in the source.

    Accepted forms, typed or not, top level or ``static`` in a class:
    ``const double ELEV_SMOOTH_SPAN_M = 60.0;``,
    ``const int _NOISE_MEDIAN_SAMPLE_CAP = 20000;`` (``20_000`` too), and for
    the difference orders a list literal: ``const _NOISE_DIFFERENCE_ORDERS =
    <int>[3, 4, 5];``.
    """
    found: Dict[str, Any] = {}
    for name in names:
        m = re.search(
            r"\bconst\s+(?:[\w<>?]+\s+)?" + re.escape(name) + r"\s*=\s*([^;]+);",
            dart_source)
        if not m:
            continue
        value = m.group(1).strip()
        listed = re.fullmatch(r"(?:const\s+)?(?:<int>)?\[([^\]]*)\]", value)
        if listed:
            found[name] = [_dart_number(v.strip())
                           for v in listed.group(1).split(",") if v.strip()]
        elif re.fullmatch(_NUMBER, value):
            found[name] = _dart_number(value)
        else:
            found[name] = value  # not a literal: reported as a mismatch
    return found


def _mismatches(dart: Dict[str, Any], python: Dict[str, Any]) -> list:
    problems = []
    for name, expected in python.items():
        if name not in dart:
            problems.append(f"{name}: not declared as a const literal")
            continue
        got = dart[name]
        if isinstance(expected, int) and not isinstance(got, int):
            problems.append(f"{name}: Dart {got!r}, Python int {expected!r}")
        elif got != expected:
            problems.append(f"{name}: Dart {got!r}, Python {expected!r}")
    return problems


def test_dart_constants_match_python():
    if not DART_SOURCE.exists():
        pytest.skip(f"{DART_SOURCE.relative_to(_ROOT)} does not exist yet "
                    "(the Dart track_metrics port, unit U13): nothing to compare")
    python = python_constants()
    dart = parse_dart_constants(DART_SOURCE.read_text(encoding="utf-8"), python)
    problems = _mismatches(dart, python)
    assert not problems, (
        "the Dart elevation-gain constants differ from src/models/track_edit.py:\n"
        + "\n".join(problems))


def test_parser_reads_every_accepted_form():
    """The parser itself, so a regex miss cannot pass as agreement once the
    Dart file lands."""
    python = python_constants()
    source = """
    // ignore_for_file: constant_identifier_names
    const double ELEV_SMOOTH_SPAN_M = 60.0;
    const ELEV_SMOOTH_MAX_SPAN_M = 240.0;
    const double ELEV_SMOOTH_TARGET_SIGMA_M = 1;
    const double ELEV_NOISE_SIGMA_MULTIPLE = 4.5;
    class _Band {
      static const double ELEV_GAIN_THRESHOLD_MIN_M = 1.0;
    }
    const double ELEV_GAIN_THRESHOLD_MAX_M = 20.0;
    const _NOISE_DIFFERENCE_ORDERS = <int>[3, 4, 5];
    const int _NOISE_MIN_DIFFERENCES = 8;
    const int _NOISE_MAX_RUN_STRIDE = 16;
    const int _NOISE_MEDIAN_SAMPLE_CAP = 20_000;
    """
    assert _mismatches(parse_dart_constants(source, python), python) == []


def test_parser_reports_a_changed_or_missing_constant():
    python = python_constants()
    source = """
    const double ELEV_SMOOTH_SPAN_M = 61.0;
    const _NOISE_DIFFERENCE_ORDERS = [3, 4];
    const double _NOISE_MIN_DIFFERENCES = 8.0;
    """
    problems = _mismatches(parse_dart_constants(source, python), python)
    joined = "\n".join(problems)
    assert "ELEV_SMOOTH_SPAN_M: Dart 61.0" in joined
    assert "_NOISE_DIFFERENCE_ORDERS: Dart [3, 4]" in joined
    assert "_NOISE_MIN_DIFFERENCES: Dart 8.0, Python int 8" in joined
    assert "ELEV_GAIN_THRESHOLD_MAX_M: not declared" in joined
    assert len(problems) == len(python)
