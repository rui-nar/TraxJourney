"""The Grafana dashboards may only query metrics this app actually exports.

Nothing else ties ``nas/grafana/provisioning/dashboards/*.json`` to the code: a
metric renamed or removed in ``src/utils/metrics.py`` leaves its panels showing
"No data" with no error anywhere. This reads every Prometheus query out of the
dashboards and checks it against a real ``/metrics`` scrape.

It also checks that every app metric family the scrape exposes is documented in
``docs/METRICS.md``, which is otherwise the same silent drift in the other
direction.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.utils import metrics as app_metrics

_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD_DIR = _ROOT / "nas" / "grafana" / "provisioning" / "dashboards"
_ALLOY_CONFIG = _ROOT / "config" / "alloy-config.river.example"
_METRICS_DOC = _ROOT / "docs" / "METRICS.md"

# Every app metric shares one namespace; the HTTP families are built from it
# (``metric_namespace`` + ``http_``), so it is derived here rather than repeated.
APP_PREFIX = app_metrics._HTTP_METRIC_PREFIX.removesuffix("http_")

# Series the dashboards may query that are not the app's own families. Anything
# not app-prefixed and not listed here fails, so a dashboard still querying the
# *old* prefix after a rename can't slip past as "not ours".
_FOREIGN_PREFIXES = (
    "node_",     # node_exporter (host-resources dashboard)
    "process_",  # prometheus_client's default process collector
    "python_",   # prometheus_client's default platform/GC collectors
)
_FOREIGN_NAMES = frozenset({"up"})

_PROMQL_KEYWORDS = frozenset({
    "by", "without", "on", "ignoring", "group_left", "group_right",
    "and", "or", "unless", "bool", "offset", "inf", "nan",
})


# ── Dashboard side ───────────────────────────────────────────────────────────

def _dashboards() -> list[Path]:
    paths = sorted(_DASHBOARD_DIR.glob("*.json"))
    assert paths, f"no dashboards found under {_DASHBOARD_DIR}"
    return paths


def _queries(node, wanted="prometheus", datasource_type=None, out=None) -> list[str]:
    """Every query string in a dashboard for datasources of type *wanted*.

    A panel's ``datasource`` applies to its ``targets``, which carry none of
    their own; templating variables carry theirs. A ``type: datasource``
    variable's ``query`` is a datasource plugin name, not a query.
    """
    if out is None:
        out = []
    if isinstance(node, list):
        for child in node:
            _queries(child, wanted, datasource_type, out)
        return out
    if not isinstance(node, dict):
        return out

    ds = node.get("datasource")
    if isinstance(ds, dict) and "type" in ds:
        datasource_type = ds["type"]
    elif isinstance(ds, str) and ds in ("prometheus", "loki"):
        datasource_type = ds
    is_datasource_variable = node.get("type") == "datasource"

    for key, value in node.items():
        if key in ("expr", "query", "definition") and isinstance(value, str):
            if datasource_type == wanted and not is_datasource_variable:
                out.append(value)
        elif isinstance(value, (dict, list)):
            _queries(value, wanted, datasource_type, out)
    return out


_LABEL_VALUES = re.compile(r"^\s*label_values\(\s*(.*?)\s*,\s*[A-Za-z_]\w*\s*\)\s*$", re.S)
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`')
_MATCHERS = re.compile(r"\{[^{}]*\}")
_RANGE = re.compile(r"\[[^\[\]]*\]")
_GROUPING = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)"
)
_GRAFANA_VARIABLE = re.compile(r"\$\{[^}]*\}|\$\w+")
_IDENTIFIER = re.compile(r"(?<![\w.])([A-Za-z_:][\w:]*)(\s*\()?")


def metric_names_in(query: str) -> set[str]:
    """Series names a PromQL query selects — no labels, functions or keywords."""
    m = _LABEL_VALUES.match(query)
    if m:
        # label_values(<selector>, <label>): the trailing argument is a label.
        query = m.group(1)
    query = _STRING.sub(" ", query)
    query = _GRAFANA_VARIABLE.sub(" ", query)
    query = _MATCHERS.sub(" ", query)       # label names inside {...}
    query = _RANGE.sub(" ", query)          # [5m]
    query = _GROUPING.sub(" ", query)       # by (le, handler)
    names = set()
    for ident, call in _IDENTIFIER.findall(query):
        if call or ident in _PROMQL_KEYWORDS:
            continue  # function name, or operator/modifier keyword
        names.add(ident)
    return names


def _dashboard_metric_names() -> dict[str, set[str]]:
    """{metric name: {dashboard file names that query it}}."""
    found: dict[str, set[str]] = {}
    for path in _dashboards():
        for query in _queries(json.loads(path.read_text(encoding="utf-8"))):
            for name in metric_names_in(query):
                found.setdefault(name, set()).add(path.name)
    return found


# ── App side ─────────────────────────────────────────────────────────────────

_TYPE_LINE = re.compile(r"^# TYPE (\S+) (\S+)$", re.M)


@pytest.fixture(scope="module")
def exported_families() -> dict[str, str]:
    """{family name as exposed: type} from a real scrape of ``api.router.app``."""
    import api.router as router

    previous = os.environ.get("METRICS_TOKEN")
    os.environ["METRICS_TOKEN"] = "contract"
    try:
        client = TestClient(router.app)
        # The HTTP instrumentator only materialises its families on first request.
        client.get("/api/version")
        resp = client.get("/metrics", headers={"Authorization": "Bearer contract"})
    finally:
        if previous is None:
            os.environ.pop("METRICS_TOKEN", None)
        else:
            os.environ["METRICS_TOKEN"] = previous
    assert resp.status_code == 200, resp.text
    return dict(_TYPE_LINE.findall(resp.text))


def _queryable_series(families: dict[str, str]) -> set[str]:
    """Every series name a query can select, given the exposed families.

    The ``# TYPE`` line names the family; histograms and summaries are only
    queryable through their ``_bucket``/``_sum``/``_count`` series, and counters
    and histograms also expose ``_created``.
    """
    series = set()
    for name, kind in families.items():
        if kind == "counter":
            series |= {name, name.removesuffix("_total") + "_created"}
        elif kind == "histogram":
            series |= {name + s for s in ("_bucket", "_sum", "_count", "_created")}
        elif kind == "summary":
            series |= {name} | {name + s for s in ("_sum", "_count", "_created")}
        else:
            series.add(name)
    return series


def _app_families(families: dict[str, str]) -> set[str]:
    """App-owned families, minus the ``*_created`` gauges prometheus_client emits
    alongside unlabelled counters/histograms — an artefact, not a metric."""
    return {
        name for name, kind in families.items()
        if name.startswith(APP_PREFIX)
        and not (kind == "gauge" and name.endswith("_created"))
    }


# ── The contract ─────────────────────────────────────────────────────────────

def test_prefix_is_derived_from_the_code():
    assert APP_PREFIX and APP_PREFIX.endswith("_")
    assert app_metrics.LOGINS._name.startswith(APP_PREFIX)


def test_dashboards_query_app_metrics_at_all():
    """Guards the extractor: a parser that finds nothing passes every check."""
    names = _dashboard_metric_names()
    app_names = {n for n in names if n.startswith(APP_PREFIX)}
    assert len(app_names) >= 20, sorted(names)
    assert f"{APP_PREFIX}http_request_duration_seconds_bucket" in app_names


def test_every_dashboard_app_metric_is_exported(exported_families):
    queryable = _queryable_series(exported_families)
    missing = {
        name: sorted(files)
        for name, files in _dashboard_metric_names().items()
        if name.startswith(APP_PREFIX) and name not in queryable
    }
    assert not missing, (
        f"dashboards query {APP_PREFIX}* series the app does not export: {missing}"
    )


def test_every_other_dashboard_metric_has_a_known_source():
    unknown = {
        name: sorted(files)
        for name, files in _dashboard_metric_names().items()
        if not name.startswith(APP_PREFIX)
        and name not in _FOREIGN_NAMES
        and not name.startswith(_FOREIGN_PREFIXES)
    }
    assert not unknown, (
        f"dashboards query series that are neither the app's ({APP_PREFIX}*) nor a "
        f"known exporter's — a stale prefix after a rename looks exactly like this: "
        f"{unknown}"
    )


def test_every_exported_app_metric_is_documented(exported_families):
    doc = _METRICS_DOC.read_text(encoding="utf-8")
    families = _app_families(exported_families)
    assert families, "scrape exposed no app metric families"
    undocumented = sorted(name for name in families if f"`{name}`" not in doc)
    assert not undocumented, f"missing from docs/METRICS.md: {undocumented}"


# ── Labels Alloy attaches ────────────────────────────────────────────────────
# ``job`` on scrape-level series (``up``, ``process_*``) and ``service`` on log
# streams are not set by the app at all but by the Alloy config on the VPS, so a
# rename there leaves the dashboards' env dropdowns and every Logs panel empty
# with no error anywhere — the same silent drift as a metric rename.

_SELECTOR = re.compile(r"([A-Za-z_:][\w:]*)?\s*\{([^{}]*)\}")


def _matcher_values(matchers: str, label: str) -> set[str]:
    return set(re.findall(rf'(?<![\w]){label}\s*=~?\s*"([^"]*)"', matchers))


def _alloy_values(pattern: str) -> set[str]:
    return set(re.findall(pattern, _ALLOY_CONFIG.read_text(encoding="utf-8")))


def test_dashboard_job_matchers_match_the_alloy_scrape_job():
    alloy_jobs = _alloy_values(r'"job"\s*=\s*"([^"]+)"')
    assert len(alloy_jobs) == 1, alloy_jobs
    # Only series the scrape itself labels: an app family's own ``job`` label
    # (``job="daily_backup"``) is an APScheduler job id, not the scrape job.
    found: dict[str, set[str]] = {}
    for path in _dashboards():
        for query in _queries(json.loads(path.read_text(encoding="utf-8"))):
            for name, matchers in _SELECTOR.findall(query):
                if name.startswith(APP_PREFIX):
                    continue
                for value in _matcher_values(matchers, "job"):
                    found.setdefault(value, set()).add(path.name)
    assert found, "no job= matcher found on a scrape-level series"
    assert set(found) == alloy_jobs, (
        f"dashboards filter on job values {found}; Alloy scrapes as {alloy_jobs}"
    )


def test_logs_dashboard_service_matches_the_alloy_log_label():
    alloy_services = _alloy_values(
        r'target_label\s*=\s*"service"\s*replacement\s*=\s*"([^"]+)"'
    )
    assert len(alloy_services) == 1, alloy_services
    found: set[str] = set()
    for path in _dashboards():
        for query in _queries(json.loads(path.read_text(encoding="utf-8")), "loki"):
            for _name, matchers in _SELECTOR.findall(query):
                found |= _matcher_values(matchers, "service")
    assert found, "no Loki service= stream selector found in any dashboard"
    assert found == alloy_services, (
        f"Loki queries select service {found}; Alloy labels streams {alloy_services}"
    )


# ── Extractor unit checks ────────────────────────────────────────────────────

@pytest.mark.parametrize("query, expected", [
    ('histogram_quantile(0.95, sum(rate(a_bucket{env="$env"}[5m])) by (le, handler))',
     {"a_bucket"}),
    ('100 * a{state="in_use",env="$env"} / b{env="$env"}', {"a", "b"}),
    ('time() - a_ts{job="daily_backup"}', {"a_ts"}),
    ('label_values(up{job="traxjourney"}, env)', {"up"}),
    ('topk(10, sum by (handler) (rate(x_total[$__rate_interval])))', {"x_total"}),
    ('rate(a_sum[5m]) / on(instance) group_left rate(a_count[5m])', {"a_sum", "a_count"}),
])
def test_metric_name_extraction(query, expected):
    assert metric_names_in(query) == expected
