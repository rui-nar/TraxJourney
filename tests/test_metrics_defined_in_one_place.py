"""Every app metric is constructed in src/utils/metrics.py (issue #437).

In multiprocess mode ``/metrics`` reads each gauge only from files in the mode
the serving process (the API) defines for it, and drops gauges it doesn't
define. A gauge built somewhere only a worker imports would be written by the
worker and silently dropped by the API. Keeping every construction in the one
module the API always imports rules that out, and keeps the catalogue in one
place for docs/METRICS.md.

Static: names bound to prometheus_client's metric classes (any import style)
must not be called outside the allowed module.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SOURCES = ("src", "api", "models", "scripts", "alembic")
_ALLOWED = {"src/utils/metrics.py"}
_METRIC_CLASSES = {"Counter", "Gauge", "Histogram", "Summary", "Info", "Enum"}


def metric_constructions(path: Path) -> list[int]:
    """Lines in *path* that call a prometheus_client metric class."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes: set[str] = set()   # local names bound to a metric class
    modules: set[str] = set()   # local names bound to prometheus_client (or a submodule)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("prometheus_client"):
            classes |= {a.asname or a.name for a in node.names if a.name in _METRIC_CLASSES}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("prometheus_client"):
                    modules.add(alias.asname or alias.name.split(".")[0])
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in classes:
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr in _METRIC_CLASSES:
            head = func.value
            while isinstance(head, ast.Attribute):
                head = head.value
            if isinstance(head, ast.Name) and head.id in modules:
                lines.append(node.lineno)
    return lines


def test_app_metrics_are_constructed_only_in_the_metrics_module():
    found = {}
    for folder in _SOURCES:
        for path in (_ROOT / folder).rglob("*.py"):
            rel = path.relative_to(_ROOT).as_posix()
            if rel in _ALLOWED:
                continue
            lines = metric_constructions(path)
            if lines:
                found[rel] = lines
    assert not found, (
        f"prometheus_client metrics constructed outside {sorted(_ALLOWED)}: {found}. "
        "Define them in src/utils/metrics.py, which the API process always imports."
    )


def test_the_allowed_module_is_seen():
    """Guards the scanner: it must find the real constructions."""
    assert len(metric_constructions(_ROOT / "src" / "utils" / "metrics.py")) >= 15


@pytest.mark.parametrize("source, flagged", [
    ("from prometheus_client import Gauge\nGauge('a', 'b')\n", True),
    ("from prometheus_client import Counter as C\nC('a', 'b')\n", True),
    ("import prometheus_client as pc\npc.Histogram('a', 'b')\n", True),
    ("import prometheus_client\nprometheus_client.metrics.Summary('a', 'b')\n", True),
    ("from prometheus_client.metrics import Gauge\nGauge('a', 'b')\n", True),
    ("from collections import Counter\nCounter('abc')\n", False),
    ("from prometheus_client import Gauge\nx = Gauge\n", False),
])
def test_the_scanner_recognises_each_import_style(tmp_path, source, flagged):
    path = tmp_path / "m.py"
    path.write_text(source)
    assert bool(metric_constructions(path)) is flagged
