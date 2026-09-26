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
    # No app family carries its own ``job`` label (see
    # test_no_app_metric_uses_a_label_the_scrape_attaches), so every ``job=``
    # matcher, on any series, is the scrape job.
    found: dict[str, set[str]] = {}
    for path in _dashboards():
        for query in _queries(json.loads(path.read_text(encoding="utf-8"))):
            for _name, matchers in _SELECTOR.findall(query):
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


# ── Labels the scrape reserves (issue #435) ──────────────────────────────────
# The scrape attaches ``job``, ``instance`` and every label on its target. When
# an app series already carries one of them, Prometheus keeps the scrape's value
# and renames the app's to ``exported_<name>`` (``honor_labels`` is off). A
# dashboard filtering on the app's label then matches nothing, with no error
# anywhere. The "Backup silently stopped" panel did exactly that on ``job``.

_LABEL_MATCHER = re.compile(
    r'([A-Za-z_]\w*)\s*(?:=~|!~|!=|=)\s*(?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`)'
)
_GROUPING_LABELS = re.compile(r"\b(?:by|without|on|ignoring)\s*\(([^()]*)\)")


def _scrape_attached_labels() -> set[str]:
    """``instance`` plus every label on the app's Alloy scrape target."""
    block = re.search(
        r'prometheus\.scrape\s+"traxjourney"\s*\{.*?targets\s*=\s*\[(.*?)\]',
        _ALLOY_CONFIG.read_text(encoding="utf-8"), re.S,
    )
    assert block, "app scrape target not found in the Alloy config"
    target = set(re.findall(r'"([A-Za-z_]\w*)"\s*=', block.group(1)))
    return {"instance"} | {name for name in target if not name.startswith("__")}


@pytest.fixture(scope="module")
def app_series_labels(exported_families) -> dict[str, frozenset[str]]:
    """{app series name: label names it carries}, read off the metric objects.

    Not from the scrape text: a labelled family with no children yet exposes
    no sample, so its labels would be unknown. Depends on ``exported_families``
    so the HTTP instrumentator's families exist.
    """
    from prometheus_client import REGISTRY

    out: dict[str, frozenset[str]] = {}
    for series, collector in REGISTRY._names_to_collectors.items():
        if not series.startswith(APP_PREFIX):
            continue
        labels = frozenset(getattr(collector, "_labelnames", ()))
        if series.endswith("_bucket") and getattr(collector, "_type", "") == "histogram":
            labels |= {"le"}
        out[series] = labels
    return out


def test_scrape_attached_labels_are_read_from_the_alloy_config():
    assert {"job", "instance", "env"} <= _scrape_attached_labels()


def test_no_app_metric_uses_a_label_the_scrape_attaches(app_series_labels):
    reserved = _scrape_attached_labels()
    clashes = {
        series: sorted(labels & reserved)
        for series, labels in app_series_labels.items()
        if labels & reserved
    }
    assert not clashes, (
        f"app metrics carry labels the scrape overwrites ({sorted(reserved)}); "
        f"Prometheus renames the app's copy to exported_<name>: {clashes}"
    )


def test_dashboards_filter_and_group_only_on_labels_the_metrics_carry(app_series_labels):
    scrape = _scrape_attached_labels()
    checked = 0
    bad: list[str] = []
    for path in _dashboards():
        for query in _queries(json.loads(path.read_text(encoding="utf-8"))):
            names = metric_names_in(query)
            for name, matchers in _SELECTOR.findall(query):
                if name not in app_series_labels:
                    continue
                for label in _LABEL_MATCHER.findall(matchers):
                    checked += 1
                    if label not in app_series_labels[name] | scrape:
                        bad.append(f"{path.name}: {name} has no label {label!r}")
            if not names or not names <= app_series_labels.keys():
                continue  # a foreign series' labels are not ours to know
            # Every app series comes from the one scrape job, so grouping them
            # by ``job`` only ever meant the app's own label, the #435 bug.
            # (A ``job=`` matcher is pinned to the scrape job by the test above.)
            carried = (scrape - {"job"}).union(*(app_series_labels[n] for n in names))
            for group in _GROUPING_LABELS.findall(_STRING.sub(" ", query)):
                for label in filter(None, (s.strip() for s in group.split(","))):
                    checked += 1
                    if label not in carried:
                        bad.append(f"{path.name}: groups on {label!r}, not on {sorted(names)}")
    assert checked >= 20, f"extractor found only {checked} label references"
    assert not bad, "\n".join(bad)


# ── Parsed log fields vs stream labels (issue #436) ──────────────────────────
# Loki renames a parsed field that collides with a stream label to
# ``<name>_extracted``, so ``| logfmt ... by (service)`` groups on the stream's
# own ``service`` (one line: the app) instead of the field the app logged.

_APP_SOURCES = ("src", "api")
_LOGFMT_KEY = re.compile(r"\b([A-Za-z_]\w*)=%")
_LOGQL_LABEL_FILTER = re.compile(r"\|\s*([A-Za-z_]\w*)\s*(?:=~|!~|!=|==|=|>=|<=|>|<)")


def _stream_labels() -> set[str]:
    """Labels every log stream carries: the Alloy relabel targets, plus
    ``service_name``, which Loki 3 derives from ``service`` on ingest."""
    return _alloy_values(r'target_label\s*=\s*"([^"]+)"') | {"service_name"}


def _app_logfmt_keys() -> set[str]:
    """``key=`` names the app writes into log lines (``"upstream=%s"``,
    ``"request_id=%(request_id)s"``)."""
    keys: set[str] = set()
    for folder in _APP_SOURCES:
        for path in (_ROOT / folder).rglob("*.py"):
            keys |= set(_LOGFMT_KEY.findall(path.read_text(encoding="utf-8")))
    return keys


def _logfmt_label_refs(query: str) -> set[str]:
    """Labels a LogQL query filters on after ``| logfmt`` or groups on."""
    parsed = query.split("| logfmt", 1)
    if len(parsed) == 1:
        return set()
    refs = set(_LOGQL_LABEL_FILTER.findall(_STRING.sub(" ", parsed[1])))
    for group in _GROUPING_LABELS.findall(_STRING.sub(" ", query)):
        refs |= {s.strip() for s in group.split(",") if s.strip()}
    return refs


def test_stream_labels_are_read_from_the_alloy_config():
    assert {"service", "env"} <= _stream_labels()


def test_app_logfmt_keys_are_found():
    """Guards the extractor: the request-context fields every line carries."""
    assert {"request_id", "user_id", "endpoint"} <= _app_logfmt_keys()


def test_no_app_log_field_shadows_a_stream_label():
    clashes = _app_logfmt_keys() & _stream_labels()
    assert not clashes, (
        f"the app logs key=value fields named like a Loki stream label, which "
        f"Loki renames to <name>_extracted: {sorted(clashes)}"
    )


def test_logs_dashboards_use_only_parsed_fields_the_app_logs():
    streams = _stream_labels()
    fields = _app_logfmt_keys()
    refs_seen = 0
    bad: list[str] = []
    for path in _dashboards():
        for query in _queries(json.loads(path.read_text(encoding="utf-8")), "loki"):
            for label in _logfmt_label_refs(query):
                refs_seen += 1
                if label in streams:
                    bad.append(f"{path.name}: {label!r} is a stream label, not the parsed field")
                elif label not in fields:
                    bad.append(f"{path.name}: {label!r} is not a field the app logs")
    assert refs_seen, "no label grouped or filtered after | logfmt in any dashboard"
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("query, expected", [
    ('sum(rate({service="x"} |= "a=b" | logfmt [5m])) by (upstream)', {"upstream"}),
    ('{service="x"} | logfmt | user_id="42" | duration > 1', {"user_id", "duration"}),
    ('sum(rate({service="x"} |= "ERROR" [5m])) by (env)', set()),
])
def test_logfmt_label_ref_extraction(query, expected):
    assert _logfmt_label_refs(query) == expected


# ── Extractor unit checks ────────────────────────────────────────────────────

@pytest.mark.parametrize("query, expected", [
    ('histogram_quantile(0.95, sum(rate(a_bucket{env="$env"}[5m])) by (le, handler))',
     {"a_bucket"}),
    ('100 * a{state="in_use",env="$env"} / b{env="$env"}', {"a", "b"}),
    ('time() - a_ts{job_name="daily_backup"}', {"a_ts"}),
    ('label_values(up{job="traxjourney"}, env)', {"up"}),
    ('topk(10, sum by (handler) (rate(x_total[$__rate_interval])))', {"x_total"}),
    ('rate(a_sum[5m]) / on(instance) group_left rate(a_count[5m])', {"a_sum", "a_count"}),
])
def test_metric_name_extraction(query, expected):
    assert metric_names_in(query) == expected


# ── Multiprocess mode, as production runs (issue #455) ───────────────────────
# The compose deployments set PROMETHEUS_MULTIPROC_DIR, and /metrics then
# serves what the per-process files hold, not the default registry. Everything
# above scrapes single-process mode, so it cannot see what goes missing, gains
# a ``pid`` label, or reads 0 only in production.

_MULTIPROC_SCRAPE = r'''
import sys
from sqlalchemy import text
from fastapi.testclient import TestClient

import api.router as router
from models.db import engine
from prometheus_client import REGISTRY
from src.api.strava_client import _SHORT_TERM_LIMITER

# Give every labelled app family a child, as production traffic eventually does.
for name, c in list(REGISTRY._names_to_collectors.items()):
    labelnames = getattr(c, "_labelnames", ())
    if name.startswith("traxjourney_") and labelnames and not getattr(c, "_labelvalues", ()):
        try:
            c.labels(*["probe"] * len(labelnames))
        except Exception:
            pass

with engine.begin() as conn:  # a database file with something in it
    conn.execute(text("CREATE TABLE probe (x INTEGER)"))
_SHORT_TERM_LIMITER.acquire()  # one Strava call this window

client = TestClient(router.app)
client.get("/api/version")
resp = client.get("/metrics", headers={"Authorization": "Bearer mp"})
assert resp.status_code == 200, resp.text
sys.stdout.write(resp.text)
'''

_SAMPLE_LINE = re.compile(r"^([A-Za-z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)$", re.M)


@pytest.fixture(scope="module")
def multiproc_scrape(tmp_path_factory) -> str:
    import subprocess
    import sys

    work = tmp_path_factory.mktemp("multiproc")
    (work / "metrics").mkdir()
    env = {k: v for k, v in os.environ.items() if k != "METRICS_TOKEN"}
    env.update(
        PROMETHEUS_MULTIPROC_DIR=str(work / "metrics"),
        METRICS_TOKEN="mp",
        DATABASE_URL=f"sqlite:///{(work / 'app.db').as_posix()}",
        PYTHONPATH=str(_ROOT),
    )
    result = subprocess.run(
        [sys.executable, "-c", _MULTIPROC_SCRAPE], cwd=work, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


def _app_samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    out = []
    for name, labels, value in _SAMPLE_LINE.findall(text):
        if name.startswith(APP_PREFIX):
            out.append((name, dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', labels or "")),
                        float(value)))
    return out


def _single(samples, name, **labels) -> float:
    found = [v for n, ls, v in samples
             if n == name and all(ls.get(k) == want for k, want in labels.items())]
    assert len(found) == 1, f"{name}{labels}: {len(found)} series, want exactly 1"
    return found[0]


# Families the app's own /metrics serves besides its prefixed ones:
# prometheus_client's default process and platform/GC collectors.
_APP_JOB_FOREIGN_PREFIXES = ("process_", "python_")


def test_multiprocess_scrape_exports_every_dashboard_metric(multiproc_scrape):
    """Every series a dashboard reads from the app's scrape job, not only the
    prefixed ones: the default registry, where prometheus_client's process
    collector lives, is never served in multiprocess mode."""
    import sys

    served = (APP_PREFIX,) + _APP_JOB_FOREIGN_PREFIXES
    if sys.platform == "win32":
        # The process collector reads /proc; there is none on Windows.
        served = tuple(p for p in served if p != "process_")
    queryable = _queryable_series(dict(_TYPE_LINE.findall(multiproc_scrape)))
    wanted = {n for n in _dashboard_metric_names() if n.startswith(served)}
    assert any(n.startswith(_APP_JOB_FOREIGN_PREFIXES) for n in wanted) or sys.platform == "win32"
    missing = {
        name: sorted(files)
        for name, files in _dashboard_metric_names().items()
        if name in wanted and name not in queryable
    }
    assert not missing, f"not exported in multiprocess mode: {missing}"


def test_multiprocess_scrape_carries_no_pid_label(multiproc_scrape):
    """A ``pid`` label means one series per process, dead ones included, and a
    ratio of a metric with it and one without matches nothing."""
    with_pid = sorted({n for n, labels, _ in _app_samples(multiproc_scrape) if "pid" in labels})
    assert not with_pid, f"app series with a pid label in multiprocess mode: {with_pid}"


def test_multiprocess_scrape_time_gauges_hold_the_live_value(multiproc_scrape):
    samples = _app_samples(multiproc_scrape)
    # pool_size 20 + max_overflow 40 on a file database (models/db.py).
    assert _single(samples, f"{APP_PREFIX}db_pool_capacity") == 60
    _single(samples, f"{APP_PREFIX}db_pool_overflow")
    _single(samples, f"{APP_PREFIX}db_pool_connections", state="in_use")
    _single(samples, f"{APP_PREFIX}db_pool_connections", state="idle")
    assert _single(samples, f"{APP_PREFIX}db_file_size_bytes", file="main") > 0
    _single(samples, f"{APP_PREFIX}db_file_size_bytes", file="wal")
    assert _single(samples, f"{APP_PREFIX}strava_rate_limit_usage", window="15min") == 1
    assert _single(samples, f"{APP_PREFIX}strava_rate_limit_capacity", window="15min") == 100


_RATIO = re.compile(r"([A-Za-z_:][\w:]*)\s*\{[^{}]*\}\s*[-+*/]\s*([A-Za-z_:][\w:]*)\s*\{")


def test_dashboard_ratios_divide_series_with_the_same_labels(multiproc_scrape):
    """``a{...} / b{...}`` with no ``on``/``ignoring`` only matches series whose
    label sets are equal; one extra label on either side and the panel is empty."""
    labelsets: dict[str, set[frozenset[str]]] = {}
    for name, labels, _ in _app_samples(multiproc_scrape):
        labelsets.setdefault(name, set()).add(frozenset(labels))
    checked = 0
    bad = []
    for path in _dashboards():
        for query in _queries(json.loads(path.read_text(encoding="utf-8"))):
            if re.search(r"\bon\s*\(", query):
                continue  # matches on named labels only; not modelled here
            ignored = {
                label.strip()
                for group in re.findall(r"\bignoring\s*\(([^()]*)\)", query)
                for label in group.split(",") if label.strip()
            }
            for left, right in _RATIO.findall(_GROUPING.sub(" ", query)):
                if not (left.startswith(APP_PREFIX) and right.startswith(APP_PREFIX)):
                    continue
                checked += 1
                lhs = {ls - ignored for ls in labelsets.get(left, set())}
                rhs = {ls - ignored for ls in labelsets.get(right, set())}
                if lhs != rhs:
                    bad.append(f"{path.name}: {left} {lhs} vs {right} {rhs}")
    assert checked, "no metric-to-metric arithmetic found in any dashboard"
    assert not bad, "\n".join(bad)
