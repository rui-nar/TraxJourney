"""No queued job may reach the Strava client (issue #455).

``traxjourney_strava_rate_limit_usage`` is a scrape-time gauge read from the
limiter of the process serving ``/metrics``: the API. That is the whole count
only because the API is the only process that calls Strava (request handlers
and their FastAPI BackgroundTasks). A job moved onto an RQ worker would call
Strava through the worker's own limiter, invisible to the gauge, and the quota
alert would under-report without any error.

Static, so it needs no Redis and no imports: every function passed to
``enqueue(...)`` is traced to the module defining it, and every import in that
module, including ones inside functions, is followed.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_STRAVA = "src.api.strava_client"
_SOURCES = ("src", "api")


def _module_path(module: str) -> Path | None:
    base = _ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_of(path: Path) -> str:
    rel = path.relative_to(_ROOT).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)


def _imports(module: str) -> set[str]:
    """Modules of this repo that *module* imports, anywhere in its body."""
    path = _module_path(module)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
    return {m for m in found if _module_path(m)}


def _import_path(start: set[str], target: str) -> list[str] | None:
    """An import chain from one of *start* to *target*, or None."""
    parent: dict[str, str] = {}
    seen: set[str] = set()
    todo = sorted(start)
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        if module == target:
            chain = [module]
            while chain[-1] in parent:
                chain.append(parent[chain[-1]])
            return chain[::-1]
        for imported in _imports(module):
            if imported not in seen:
                parent.setdefault(imported, module)
                todo.append(imported)
    return None


def _enqueued_function_modules() -> dict[str, set[str]]:
    """{module defining a queued function: {function names}}."""
    out: dict[str, set[str]] = {}
    for folder in _SOURCES:
        for path in (_ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            caller = _module_of(path)
            imported_from = {
                alias.asname or alias.name: node.module
                for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
                for alias in node.names
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or len(node.args) < 2:
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name not in ("enqueue", "enqueue_call"):
                    continue
                func = node.args[1]
                if not isinstance(func, ast.Name):
                    continue  # rq's own queue.enqueue(wrapper, ...) in src/jobs/queue.py
                defining = imported_from.get(func.id, caller)
                if not _module_path(defining):
                    defining = caller
                out.setdefault(defining, set()).add(func.id)
    return out


def test_finds_the_queued_jobs():
    """Guards the scanner: a scan that finds nothing passes trivially."""
    modules = _enqueued_function_modules()
    names = set().union(*modules.values())
    assert {"_resolve_route_job", "run_poster_job", "_refresh_share_tiles"} <= names, modules
    assert modules.get("src.poster.poster_job_runner") == {"run_poster_job"}


def test_the_import_trace_can_reach_strava():
    """Guards the tracer: the API's own Strava router does import the client."""
    assert _import_path({"api.strava"}, _STRAVA)


def test_no_queued_job_reaches_the_strava_client():
    start = set(_enqueued_function_modules()) | {"src.jobs.queue", "src.jobs.worker"}
    chain = _import_path(start, _STRAVA)
    assert chain is None, (
        "a module run by an RQ worker imports the Strava client: "
        + " -> ".join(chain)
        + ". The Strava usage gauge only sees the API process' limiter "
        "(src/utils/metrics.py, STRAVA_RATE_LIMIT_USAGE); move the limiter "
        "somewhere shared before moving Strava work into a job."
    )
